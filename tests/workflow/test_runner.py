import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AdapterStatus, AgentResult, AgentTimeoutError
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.goals import GoalError, GoalLedger
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig, RoleConfig
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.domain.enums import RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.checkpoints import input_hashes
from dev_orchestration.workflow.cloud import link_cloud_session
from dev_orchestration.workflow.recovery import ResumeRefused, approve_run, resume_run
from tests.workflow.run_helpers import execute_run

CRITERIA = ["the flag exists"]
CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "demo", "class": "internal_utility"},
        "scope": {"include": ["src/"], "exclude": []},
        "context": {"persistent": []},
        "validation": {"unit": {"command": "true", "required_for": ["trivial", "standard"]}},
    }
)
PLAN = "# Plan\n"
PASS = {"outcome": "PASS", "findings": []}
BLOCKING = {
    "outcome": "CHANGES_REQUIRED",
    "findings": [
        {"id": "x", "severity": "blocking", "summary": "broken", "evidence_required": "tests"}
    ],
}
VERIFY = {
    "outcome": "PASS",
    "verdicts": [{"criterion_id": "AC1", "verdict": "PASS", "evidence": "seen"}],
}


class Scripted:
    def __init__(self, script):
        self.script = {role: list(values) for role, values in script.items()}
        self.seen = []
        self.requests = []

    def healthcheck(self):
        return AdapterStatus(name="fake", available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.seen.append(request.role)
        self.requests.append(request)
        if request.role == "implementation_worker":
            (request.cwd / "src" / "app.py").write_text("x = 2\n")
        values = self.script[request.role]
        payload = values.pop(0) if len(values) > 1 else values[0]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, payload, now, now)


ROLES = (
    "classifier",
    "planner",
    "plan_reviewer",
    "plan_reconciler",
    "implementation_worker",
    "implementation_reviewer",
    "verifier",
)


def registry(adapter, worker_timeout=None):
    return RoleRegistry(
        roles={
            role: RoleConfig(
                adapter="fake",
                model="m",
                timeout_seconds=worker_timeout if role == "implementation_worker" else None,
            )
            for role in ROLES
        },
        adapters={"fake": adapter},
    )


def script(tier="standard", review=PASS, verifier=VERIFY, profiles=None):
    return {
        "classifier": [{"tier": tier, "rationale": "r", "profiles": profiles or []}],
        "planner": [PLAN],
        "plan_reviewer": [PASS],
        "plan_reconciler": [PLAN],
        "implementation_worker": ["done"],
        "implementation_reviewer": [review],
        "verifier": [verifier],
    }


class NoOpWorker(Scripted):
    """A worker that returns success without changing the worktree."""

    def run(self, request):
        if request.role == "implementation_worker":
            self.seen.append(request.role)
            self.requests.append(request)
            now = datetime.now(UTC)
            return AgentResult("fake", None, 0, self.script[request.role][0], now, now)
        return super().run(request)


class NonzeroWorker(Scripted):
    def run(self, request):
        result = super().run(request)
        if request.role == "implementation_worker":
            return AgentResult(
                result.provider,
                result.model,
                7,
                result.output,
                result.started_at,
                result.completed_at,
            )
        return result


class TimeoutWorker(Scripted):
    def run(self, request):
        if request.role == "implementation_worker":
            raise AgentTimeoutError(request.role, request.timeout_seconds or 0, "fake")
        return super().run(request)


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".ai/runs/\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(root)


def events(outcome):
    return [
        json.loads(line) for line in (outcome.store_root / "events.jsonl").read_text().splitlines()
    ]


def test_standard_run_reaches_complete_local_and_persists_final_commit(tmp_path):
    git = repo(tmp_path)
    base = git.current_commit()
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    manifest = outcome.store_root.joinpath("manifest.json").read_text()
    assert json.loads(manifest)["git"]["final_commit"]
    assert GitRepo(tmp_path / "wt" / "demo" / outcome.run_id).is_ancestor(base)


def test_fresh_planner_receives_the_configured_scope_fence(tmp_path):
    git = repo(tmp_path)
    scope = CONFIG.scope.model_copy(
        update={"include": ["src/", "docs/"], "exclude": ["docs/private/"]}
    )
    config = CONFIG.model_copy(update={"scope": scope})
    adapter = Scripted(script())

    outcome = execute_run(git, config, registry(adapter), "add a flag", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    planner = next(request for request in adapter.requests if request.role == "planner")
    scope_item = next(item for item in planner.context.items if item.label == "scope_fence")
    assert '"include": ["src/", "docs/"]' in scope_item.content
    assert '"exclude": ["docs/private/"]' in scope_item.content


def test_missing_required_validation_dependency_fails_before_verifier(tmp_path):
    git = repo(tmp_path)
    data = CONFIG.model_dump(mode="json")
    data["validation_links"] = [".venv", "node_modules"]
    data["required_validation_links"] = ["node_modules"]
    config = ProjectConfig.model_validate(data)
    adapter = Scripted(script())
    outcome = execute_run(git, config, registry(adapter), "add a flag", tmp_path / "wt", CRITERIA)
    manifest = RunManifest.model_validate_json(
        (outcome.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert outcome.final_state is RunState.FAILED
    assert manifest.terminal_cause == "environment"
    assert "verifier" not in adapter.seen


def test_substantial_run_reaches_complete_local_when_approval_is_not_required(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {"validation": {"unit": {"command": "true", "required_for": ["substantial"]}}}
    )
    adapter = Scripted(script(tier="substantial"))
    outcome = execute_run(
        git, config, registry(adapter), "change the flag substantially", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    manifest = RunManifest.model_validate_json(
        (outcome.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.goal_attempt == 1
    assert git.branch_exists(manifest.git.branch)
    assert manifest.tier is Tier.SUBSTANTIAL
    assert not manifest.approval_required
    assert adapter.seen == [
        "classifier",
        "planner",
        "plan_reviewer",
        "implementation_worker",
        "implementation_reviewer",
        "verifier",
    ]


def test_substantial_run_pauses_before_worker_when_approval_is_required(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json") | {"approval": {"substantial": True}}
    )
    adapter = Scripted(script(tier="substantial"))
    outcome = execute_run(
        git,
        config,
        registry(adapter),
        "change the flag with approval",
        tmp_path / "wt",
        CRITERIA,
        goal_id="approval-goal",
    )
    assert outcome.final_state is RunState.AWAITING_APPROVAL
    assert outcome.reason == "human approval required before execution"
    assert adapter.seen == ["classifier", "planner", "plan_reviewer"]
    manifest = RunManifest.model_validate_json(
        (outcome.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.approval_required
    assert manifest.git.final_commit is None
    summary = outcome.store_root / "approval" / "human-approval.md"
    assert summary.is_file()
    assert "change the flag with approval" in summary.read_text(encoding="utf-8")
    assert any(event["event"] == "approval_required" for event in events(outcome))
    assert Path(manifest.git.worktree).exists()
    with pytest.raises(GoalError, match="already has active run"):
        execute_run(
            git,
            config,
            registry(Scripted(script(tier="substantial"))),
            "change the flag with approval",
            tmp_path / "wt",
            CRITERIA,
            goal_id="approval-goal",
        )


def test_linked_cloud_run_requires_explicit_approval_then_resumes_saved_plan(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "approval": {"substantial": True},
            "validation": {"unit": {"command": "true", "required_for": ["substantial"]}},
        }
    )
    adapter = Scripted(script(tier="substantial"))
    roles = registry(adapter)
    outcome = execute_run(
        git, config, roles, "change the flag with approval", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.AWAITING_APPROVAL
    assert adapter.seen == ["classifier", "planner", "plan_reviewer"]
    with pytest.raises(ResumeRefused, match="link the intact cloud session"):
        approve_run(git, config, roles, outcome.run_id)
    with pytest.raises(ResumeRefused, match="awaiting explicit human approval"):
        resume_run(git, config, roles, outcome.run_id)

    store = RunStore(git.root, outcome.run_id)
    saved_worktree = Path(store.read_manifest().git.worktree)
    link_cloud_session(
        git,
        outcome.run_id,
        "claude",
        "session-1",
        "environment-1",
        restored_worktree=saved_worktree,
    )
    plan_before = (store.root / "planning/approved-plan.md").read_bytes()
    digest = approve_run(
        git,
        config,
        roles,
        outcome.run_id,
        restored_worktree=saved_worktree,
    )
    assert store.read_manifest().status is RunState.APPROVED
    assert len(digest) == 64
    proof = json.loads((store.root / "approval/approved.json").read_text())
    assert proof["plan_sha256"] == store.read_manifest().plan_sha256
    assert proof["cloud_session_id"] == "session-1"
    with pytest.raises(ResumeRefused, match="needs the checkpoint digest"):
        resume_run(git, config, roles, outcome.run_id)
    with pytest.raises(ResumeRefused, match="not awaiting human approval"):
        approve_run(git, config, roles, outcome.run_id)

    resumed = resume_run(
        git,
        config,
        roles,
        outcome.run_id,
        expected_checkpoint_digest=digest,
        restored_worktree=saved_worktree,
    )
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == [
        "classifier",
        "planner",
        "plan_reviewer",
        "implementation_worker",
        "implementation_reviewer",
        "verifier",
    ]
    assert (store.root / "planning/approved-plan.md").read_bytes() == plan_before


def test_cloud_approval_refuses_changed_summary_before_provider_call(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json") | {"approval": {"substantial": True}}
    )
    adapter = Scripted(script(tier="substantial"))
    roles = registry(adapter)
    outcome = execute_run(
        git, config, roles, "change the flag with approval", tmp_path / "wt", CRITERIA
    )
    link_cloud_session(git, outcome.run_id, "codex", "task-1", "environment-1")
    summary = outcome.store_root / "approval/human-approval.md"
    summary.write_text("changed summary")
    with pytest.raises(ResumeRefused, match="checkpoint artifact"):
        approve_run(git, config, roles, outcome.run_id)
    assert adapter.seen == ["classifier", "planner", "plan_reviewer"]


def test_complete_local_persists_clean_final_commit(tmp_path):
    git = repo(tmp_path)
    base = git.current_commit()
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script())),
        "change the flag",
        tmp_path / "wt",
        CRITERIA,
    )
    worktree_repo = GitRepo(tmp_path / "wt" / "demo" / outcome.run_id)
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    final = manifest["git"]["final_commit"]
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert worktree_repo.status_paths() == []
    assert final and final != base
    assert worktree_repo.is_ancestor(base, final)
    assert (
        "x = 2"
        in subprocess.run(
            ["git", "-C", str(worktree_repo.root), "show", f"{final}:src/app.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_trivial_executor_receives_request_scope_criteria_and_constraints(tmp_path):
    git = repo(tmp_path)
    adapter = Scripted(script(tier="trivial"))
    outcome = execute_run(git, CONFIG, registry(adapter), "fix a typo", tmp_path / "wt", CRITERIA)
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "planner" not in adapter.seen
    executor = next(
        request for request in adapter.requests if request.role == "implementation_worker"
    )
    rendered = executor.context.render()
    assert "fix a typo" in rendered and "Protected constraints" in rendered
    assert (outcome.store_root / "planning" / "task-contract.md").is_file()


def test_opted_in_trivial_run_discovers_declared_sources_and_supplies_report(tmp_path):
    git = repo(tmp_path)
    (git.root / "docs" / "specs").mkdir(parents=True)
    (git.root / "docs" / "specs" / "architecture.md").write_text(
        "Current shared interface: CustomerId.\n"
    )
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "architecture"], check=True)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "context": {
                "persistent": [],
                "on_demand": {"specifications": "docs/specs"},
                "dependency_discovery": True,
            }
        }
    )
    report = {
        "findings": [
            {
                "relationship": "shared_contract",
                "source": "docs/specs/architecture.md",
                "excerpt": "CustomerId",
                "current_behavior": "A caller uses this interface.",
                "future_work": "The task may affect it.",
                "approval_status": "not_applicable",
                "effect_on_task": "Preserve caller compatibility.",
            }
        ],
        "searched_sources": [],
        "inaccessible_sources": [],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }
    scripted = script(tier="trivial")
    scripted["dependency_analyst"] = [report]
    adapter = Scripted(scripted)
    roles = registry(adapter)
    roles = RoleRegistry(
        roles={
            **roles.roles,
            "dependency_analyst": RoleConfig(adapter="fake", model="m"),
        },
        adapters=roles.adapters,
    )

    outcome = execute_run(
        git, config, roles, "make the interface change", tmp_path / "wt", CRITERIA
    )

    saved = json.loads((outcome.store_root / "dependency-report.json").read_text())
    store = RunStore(git.root, outcome.run_id)
    worker = next(item for item in adapter.requests if item.role == "implementation_worker")
    verifier = next(item for item in adapter.requests if item.role == "verifier")
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "dependency_analyst" in adapter.seen
    assert "CustomerId" in worker.context.render() and "CustomerId" in verifier.context.render()
    assert saved["source_identities"][0]["path"] == "docs/specs/architecture.md"
    assert "dependency-report.json" in store.latest_checkpoint()["artifacts"]
    (git.root / "docs" / "specs" / "architecture.md").write_text("Changed contract.\n")
    with pytest.raises(CheckpointError, match="changed after discovery"):
        input_hashes(store, roles)
    input_hashes(store, roles, allow_changed_dependency_sources=True)


def test_resume_loads_dependency_report_and_discovers_after_classification(tmp_path):
    class PauseFirstPlanner(Scripted):
        paused = False

        def run(self, request):
            if request.role == "planner" and not self.paused:
                self.paused = True
                self.seen.append(request.role)
                self.requests.append(request)
                raise AgentTimeoutError(request.role, 7, "fake")
            return super().run(request)

    git = repo(tmp_path)
    (git.root / "docs" / "specs").mkdir(parents=True)
    (git.root / "docs" / "specs" / "contract.md").write_text("Shared token: AccountId.\n")
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "contract"], check=True)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "context": {
                "persistent": [],
                "on_demand": {"specifications": "docs/specs"},
                "dependency_discovery": True,
            }
        }
    )
    report = {
        "findings": [
            {
                "relationship": "shared_contract",
                "source": "docs/specs/contract.md",
                "excerpt": "AccountId",
                "current_behavior": "The contract names AccountId.",
                "future_work": "The task may touch this contract.",
                "approval_status": "not_applicable",
                "effect_on_task": "Preserve compatibility.",
            }
        ],
        "searched_sources": [],
        "inaccessible_sources": [],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }
    scripted = script()
    scripted["dependency_analyst"] = [report]
    adapter = PauseFirstPlanner(scripted)
    roles = registry(adapter)
    roles = RoleRegistry(
        roles={
            **roles.roles,
            "dependency_analyst": RoleConfig(adapter="fake", model="m"),
        },
        adapters=roles.adapters,
    )

    paused = execute_run(git, config, roles, "update account behavior", tmp_path / "wt", CRITERIA)
    assert paused.final_state is RunState.PAUSED_INTERRUPTED
    assert adapter.seen.index("classifier") < adapter.seen.index("dependency_analyst")

    resumed = resume_run(git, config, roles, paused.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    planner_requests = [item for item in adapter.requests if item.role == "planner"]
    assert len(planner_requests) == 2
    assert "AccountId" in planner_requests[-1].context.render()


def test_imported_plan_is_reviewed_with_dependency_report(tmp_path):
    # The fixture supplies semantic findings; this asserts framework propagation, not model recall.
    git = repo(tmp_path)
    (git.root / "src" / "app.py").write_text('customer_key = "CustomerKey"\n')
    (git.root / "docs" / "plans").mkdir(parents=True)
    (git.root / "docs" / "plans" / "epic-a.md").write_text(
        "Epic A links to epic-b.md and shares CustomerId with the existing code.\n"
    )
    (git.root / "docs" / "plans" / "epic-b.md").write_text(
        "Approved future work: keep CustomerId stable.\n"
    )
    (git.root / "docs" / "plans" / "epic-c.md").write_text("Draft proposal: replace CustomerId.\n")
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "plans"], check=True)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "context": {
                "persistent": [],
                "on_demand": {"plans": "docs/plans", "code": "src"},
                "dependency_discovery": True,
            }
        }
    )
    report = {
        "findings": [
            {
                "relationship": "shared_contract",
                "source": "docs/plans/epic-a.md",
                "excerpt": "shares CustomerId",
                "current_behavior": "Existing code uses CustomerId.",
                "future_work": "Epic B keeps that name stable.",
                "approval_status": "approved",
                "effect_on_task": "Preserve the interface.",
            },
            {
                "relationship": "prerequisite",
                "source": "docs/plans/epic-b.md",
                "excerpt": "Approved future work",
                "current_behavior": "The follow-up is not implemented yet.",
                "future_work": "Approved work depends on a stable CustomerId.",
                "approval_status": "approved",
                "effect_on_task": "Keep this prerequisite ordered after the current change.",
            },
            {
                "relationship": "conflict",
                "source": "src/app.py",
                "excerpt": "CustomerKey",
                "current_behavior": "Current code exposes CustomerKey.",
                "future_work": "The task changes callers to CustomerId.",
                "approval_status": "not_applicable",
                "effect_on_task": "Update the implementation and callers together.",
            },
            {
                "relationship": "advisory",
                "source": "docs/plans/epic-c.md",
                "excerpt": "Draft proposal",
                "current_behavior": "Existing code uses CustomerId.",
                "future_work": "Draft suggests replacing it.",
                "approval_status": "draft",
                "effect_on_task": "No required change.",
            },
        ],
        "searched_sources": [],
        "inaccessible_sources": [],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }
    scripted = script()
    scripted["dependency_analyst"] = [report]
    adapter = Scripted(scripted)
    roles = registry(adapter)
    roles = RoleRegistry(
        roles={
            **roles.roles,
            "dependency_analyst": RoleConfig(adapter="fake", model="m"),
        },
        adapters=roles.adapters,
    )
    external_plan = tmp_path / "external-plan.md"
    external_plan.write_text("# Imported plan\n")

    outcome = execute_run(
        git,
        config,
        roles,
        "implement the approved plan",
        tmp_path / "wt",
        CRITERIA,
        external_plan=external_plan,
    )

    reviewer = next(item for item in adapter.requests if item.role == "plan_reviewer")
    implementation_reviewer = next(
        item for item in adapter.requests if item.role == "implementation_reviewer"
    )
    saved = json.loads((outcome.store_root / "dependency-report.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "planner" not in adapter.seen and "dependency_analyst" in adapter.seen
    assert "Draft proposal" in reviewer.context.render()
    assert "Draft proposal" in implementation_reviewer.context.render()
    assert "CustomerKey" in implementation_reviewer.context.render()
    assert "approved" in implementation_reviewer.context.render()
    assert {item["path"] for item in saved["source_identities"]} == {
        "docs/plans/epic-a.md",
        "docs/plans/epic-b.md",
        "docs/plans/epic-c.md",
        "src/app.py",
    }


def test_implementing_declared_spec_gets_standard_path_and_classifier_context(tmp_path):
    git = repo(tmp_path)
    (git.root / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (git.root / "docs" / "superpowers" / "specs" / "feature.md").write_text(
        "Add the feature behavior.\n"
    )
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "spec"], check=True)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "context": {
                "persistent": [],
                "on_demand": {"specifications": "docs/superpowers/specs"},
            }
        }
    )
    adapter = Scripted(script(tier="trivial"))
    outcome = execute_run(
        git,
        config,
        registry(adapter),
        "Implement docs/superpowers/specs/feature.md",
        tmp_path / "wt",
        CRITERIA,
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    classifier = next(item for item in adapter.requests if item.role == "classifier")
    rendered = classifier.context.render()
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "standard"
    assert "Add the feature behavior." in rendered
    assert json.dumps(CRITERIA) in rendered
    assert "planner" in adapter.seen and "implementation_reviewer" in adapter.seen
    assert any(event["event"] == "classification_tier_raised" for event in events(outcome))


def test_editing_spec_wording_retains_trivial_path(tmp_path):
    git = repo(tmp_path)
    adapter = Scripted(script(tier="trivial"))
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "Correct a sentence in docs/spec.md",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "planner" not in adapter.seen


def test_profile_minimum_tier_and_protected_rules_reach_runtime(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        {
            **CONFIG.model_dump(mode="json"),
            "profiles": {
                "available": ["auth"],
                "definitions": {
                    "auth": {
                        "minimum_tier": "standard",
                        "controls": {"security_review": "required"},
                    }
                },
            },
        }
    )
    adapter = Scripted(script(tier="trivial", profiles=["auth"]))
    outcome = execute_run(
        git, config, registry(adapter), "secure the flag", tmp_path / "wt", CRITERIA
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "standard"
    assert manifest["available_profiles"] == ["auth"]
    assert manifest["active_profiles"] == ["auth"]
    assert manifest["minimum_tier"] == "standard"
    worker = next(
        request for request in adapter.requests if request.role == "implementation_worker"
    )
    rendered = worker.context.render()
    assert "security_review" in rendered and "protected.autonomous_push" in rendered


def test_failing_validation_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {"validation": {"unit": {"command": "exit 3", "required_for": ["standard"]}}}
    )
    adapter = Scripted(script())
    outcome = execute_run(git, config, registry(adapter), "bad", tmp_path / "wt", CRITERIA)
    assert outcome.final_state is RunState.FAILED
    assert "validation" in outcome.reason
    assert outcome.store_root.joinpath("execution/validation-repair-v1.json").is_file()
    data = json.loads(
        next((outcome.store_root / "execution").glob("validation-v1.json")).read_text()
    )
    assert data[0]["exit_code"] == 3
    assert not any(event["event"] == "implementation_reviewed" for event in events(outcome))
    assert "verifier" not in adapter.seen
    assert not (outcome.store_root / "verification/final-verification.json").exists()
    assert not (outcome.store_root / "execution/execution-summary.json").exists()
    assert json.loads((outcome.store_root / "manifest.json").read_text())["terminal_cause"] == (
        "validation_failed"
    )


def test_failed_validation_gets_one_repair_then_uses_normal_review_path(tmp_path):
    class RepairingWorker(Scripted):
        worker_calls = 0

        def run(self, request):
            result = super().run(request)
            if request.role == "implementation_worker":
                self.worker_calls += 1
                if self.worker_calls == 1:
                    (request.cwd / "src" / "app.py").write_text("x = 1\n")
                    (request.cwd / "src" / "initial-attempt.txt").write_text("attempted\n")
            return result

    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": "test \"$(cat src/app.py)\" = 'x = 2'",
                    "required_for": ["standard"],
                }
            }
        }
    )
    adapter = RepairingWorker(script())
    outcome = execute_run(
        git, config, registry(adapter), "repair the flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    assert adapter.seen.count("planner") == 1
    assert adapter.seen[-2:] == ["implementation_reviewer", "verifier"]
    assert (outcome.store_root / "execution/validation-repair-v1.json").is_file()
    assert (outcome.store_root / "execution/validation-v2.json").is_file()
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["validation_repairs_reserved"] == 1
    assert manifest["validation_artifact"] == "execution/validation-v2.json"


def test_validation_repairs_are_two_per_worker_change_pooled_across_the_run(tmp_path):
    class ReviewThenBreak(Scripted):
        def __init__(self, scripts):
            super().__init__(scripts)
            self.worker_calls = 0

        def run(self, request):
            if request.role == "implementation_worker":
                self.worker_calls += 1
                self.seen.append(request.role)
                if Category.BLOCKING_FINDINGS in request.context.labels():
                    (request.cwd / "src" / "app.py").write_text("x = 1\n")
                else:
                    (request.cwd / "src" / "app.py").write_text(
                        "x = 2\n" if self.worker_calls == 2 else "x = 1\n"
                    )
                    if self.worker_calls == 1:
                        (request.cwd / "src" / "initial-attempt.txt").write_text("attempted\n")
                payloads = self.script[request.role]
                payload = payloads.pop(0) if len(payloads) > 1 else payloads[0]
                now = datetime.now(UTC)
                return AgentResult("fake", None, 0, payload, now, now)
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    "fake",
                    None,
                    0,
                    BLOCKING if self.seen.count(request.role) == 1 else PASS,
                    now,
                    now,
                )
            return super().run(request)

    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": "test \"$(cat src/app.py)\" = 'x = 2'",
                    "required_for": ["standard"],
                }
            }
        }
    )
    adapter = ReviewThenBreak(script())
    outcome = execute_run(
        git, config, registry(adapter), "repair then remediate", tmp_path / "wt", CRITERIA
    )
    # The allowance is two per worker change, pooled: the first change used one of its two, so
    # the remediation change that breaks validation may use the remaining three (v2-v4). None
    # fixes it, and no fifth is reserved.
    assert outcome.final_state is RunState.FAILED
    assert adapter.worker_calls == 6
    assert adapter.seen.count("implementation_reviewer") == 1
    assert (outcome.store_root / "execution/validation-repair-v4.json").is_file()
    assert not (outcome.store_root / "execution/validation-repair-v5.json").exists()
    assert "verifier" not in adapter.seen


@pytest.mark.parametrize(
    "remediation_action", ["noop", "stage_identical", "empty_commit", "cache", "chmod"]
)
def test_unchanged_implementation_remediation_stops_before_another_review(
    tmp_path, remediation_action
):
    class NoProgressRemediation(Scripted):
        reviewer_calls = 0

        def run(self, request):
            if (
                request.role == "implementation_worker"
                and Category.BLOCKING_FINDINGS in request.context.labels()
            ):
                self.seen.append(request.role)
                self.requests.append(request)
                if remediation_action == "stage_identical":
                    subprocess.run(["git", "-C", str(request.cwd), "add", "src/app.py"], check=True)
                elif remediation_action == "empty_commit":
                    GitRepo(request.cwd).run_git("commit", "--allow-empty", "-m", "no change")
                elif remediation_action == "cache":
                    cache = request.cwd / ".pytest_cache" / "v" / "cache"
                    cache.mkdir(parents=True)
                    (cache / "nodeids").write_text("cache")
                elif remediation_action == "chmod":
                    (request.cwd / "src" / "app.py").chmod(0o600)
                now = datetime.now(UTC)
                return AgentResult("fake", None, 0, "I made no changes", now, now)
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                self.reviewer_calls += 1
                now = datetime.now(UTC)
                return AgentResult(
                    "fake", None, 0, BLOCKING if self.reviewer_calls == 1 else PASS, now, now
                )
            return super().run(request)

    git = repo(tmp_path)
    adapter = NoProgressRemediation(script())

    outcome = execute_run(
        git, CONFIG, registry(adapter), "repair the reviewed issue", tmp_path / "wt", CRITERIA
    )

    manifest = RunManifest.model_validate_json(
        (outcome.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert outcome.final_state is RunState.FAILED
    assert manifest.terminal_cause == "remediation_no_progress"
    assert manifest.failure_class == "product"
    assert manifest.failure_signature.startswith("remediation_no_progress:")
    assert adapter.seen.count("implementation_worker") == 2
    assert adapter.seen.count("implementation_reviewer") == 1
    assert "verifier" not in adapter.seen
    assert (outcome.store_root / "execution/no-progress/remediation-v1.json").is_file()
    patch = (outcome.store_root / "execution/final-change.patch").read_bytes()
    assert b"x = 2" in patch
    assert git.verify_patch(manifest.git.base_commit, patch)


def test_unignored_validation_cache_is_cleaned_and_does_not_trip_the_fence(tmp_path):
    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": (
                        "mkdir -p .pytest_cache/v/cache && "
                        "printf cache > .pytest_cache/v/cache/nodeids"
                    ),
                    "required_for": ["standard"],
                }
            }
        }
    )
    outcome = execute_run(
        git, config, registry(Scripted(script())), "cache safely", tmp_path / "wt", CRITERIA
    )
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert not (worktree / ".pytest_cache").exists()


def test_role_timeout_pauses_run_with_role_and_limit(tmp_path):
    git = repo(tmp_path)
    goal_id = "timeout-goal"
    outcome = execute_run(
        git,
        CONFIG,
        registry(TimeoutWorker(script()), worker_timeout=7),
        "timeout safely",
        tmp_path / "wt",
        CRITERIA,
        goal_id=goal_id,
    )
    assert outcome.final_state is RunState.PAUSED_INTERRUPTED
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    pause = json.loads((outcome.store_root / "manifest.json").read_text())["pause"]
    assert pause["role"] == "implementation_worker"
    assert "7-second" in pause["diagnostic"]
    assert manifest.goal_attempt == 1
    assert Path(manifest.git.worktree).exists()
    assert git.branch_exists(manifest.git.branch)
    next_adapter = Scripted(script())
    with pytest.raises(GoalError, match="already has active run"):
        execute_run(
            git,
            CONFIG,
            registry(next_adapter),
            "timeout safely",
            tmp_path / "wt",
            CRITERIA,
            goal_id=goal_id,
        )
    assert next_adapter.seen == []


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_fail_or_unknown_verification_cannot_complete_local(tmp_path, verdict):
    git = repo(tmp_path)
    failed = dict(
        VERIFY,
        outcome="CHANGES_REQUIRED",
        verdicts=[{"criterion_id": "AC1", "verdict": verdict, "evidence": "no"}],
    )
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(verifier=failed))), "bad", tmp_path / "wt", CRITERIA
    )
    # FAIL goes to a repair that changes nothing (FAILED); an UNKNOWN that names no missing
    # test cannot be repaired by the worker (ESCALATED). Neither completes.
    expected = RunState.FAILED if verdict == "FAIL" else RunState.ESCALATED
    assert outcome.final_state is expected
    saved = json.loads((outcome.store_root / "verification/final-verification.json").read_text())
    assert saved["outcome_explanation"] == []
    assert saved["unresolved_finding_ids"] == []


def test_named_acceptance_document_note_in_evidence_does_not_block_a_valid_pass(tmp_path):
    class AcceptanceDocumentWorker(Scripted):
        def run(self, request):
            result = super().run(request)
            if request.role == "implementation_worker":
                document = request.cwd / "docs" / "evaluation-plan.md"
                document.parent.mkdir(parents=True)
                document.write_text("The requested flag is available.\n")
            elif request.role == "verifier":
                result.output["verdicts"][0]["evidence"] = (
                    "docs/evaluation-plan.md was updated with the requested note; "
                    "the supplied criterion is satisfied."
                )
            return result

    git = repo(tmp_path)
    config = CONFIG.model_copy(
        update={"scope": CONFIG.scope.model_copy(update={"include": ["src/", "docs/"]})}
    )
    outcome = execute_run(
        git,
        config,
        registry(AcceptanceDocumentWorker(script())),
        "Add the requested flag and update docs/evaluation-plan.md with a short availability note.",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    saved = json.loads((outcome.store_root / "verification/final-verification.json").read_text())
    assert saved["verdicts"][0]["evidence"].startswith("docs/evaluation-plan.md was updated")
    assert saved["outcome_explanation"] == []
    diff = (outcome.store_root / "verification/current-state-diff-v1.patch").read_text()
    assert "docs/evaluation-plan.md" in diff


def test_minor_only_verification_can_complete_and_retains_its_note(tmp_path):
    git = repo(tmp_path)
    review = {
        "outcome": "PASS",
        "findings": [{"id": "F001", "severity": "minor", "summary": "small cleanup"}],
    }
    verifier = dict(
        VERIFY,
        outcome="CHANGES_REQUIRED",
        finding_dispositions=[
            {"finding_id": "F001", "status": "unresolved", "evidence": "cleanup is non-blocking"}
        ],
    )
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=review, verifier=verifier))),
        "finish with minor note",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    result = json.loads((outcome.store_root / "verification/final-verification.json").read_text())
    assert result["outcome_explanation"] == [
        {"finding_id": "F001", "note": "cleanup is non-blocking"}
    ]


def test_blocked_escalated_and_nonzero_agent_results_are_terminal(tmp_path):
    git = repo(tmp_path)
    blocked = dict(PASS, outcome="BLOCKED")
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=blocked))),
        "bad",
        tmp_path / "wt",
        CRITERIA,
        goal_id="blocked-goal",
    )
    assert outcome.final_state is RunState.BLOCKED
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert Path(manifest.git.worktree).exists()
    assert git.branch_exists(manifest.git.branch)
    with pytest.raises(GoalError, match="already has active run"):
        execute_run(
            git,
            CONFIG,
            registry(Scripted(script())),
            "bad",
            tmp_path / "wt",
            CRITERIA,
            goal_id="blocked-goal",
        )
    escalated = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "bad",
        tmp_path / "wt",
        CRITERIA,
    )
    assert escalated.final_state is RunState.ESCALATED
    assert RunStore(git.root, escalated.run_id).read_manifest().failure_class == "human_decision"
    nonzero = execute_run(
        git,
        CONFIG,
        registry(NonzeroWorker(script())),
        "bad",
        tmp_path / "wt",
        CRITERIA,
    )
    assert nonzero.final_state is RunState.ESCALATED
    nonzero_manifest = RunStore(git.root, nonzero.run_id).read_manifest()
    assert nonzero_manifest.failure_reason
    assert nonzero_manifest.failure_class == "environment"
    assert nonzero_manifest.terminal_cause == "environment"


def test_three_product_failures_stop_the_goal_until_human_reopen(tmp_path):
    git = repo(tmp_path)
    goal_id = "epic-088"
    for expected_attempt in range(1, 4):
        outcome = execute_run(
            git,
            CONFIG,
            registry(NoOpWorker(script(review=BLOCKING))),
            "add a flag",
            tmp_path / "wt",
            CRITERIA,
            goal_id=goal_id,
        )
        manifest = RunStore(git.root, outcome.run_id).read_manifest()
        assert outcome.final_state is RunState.FAILED
        assert manifest.goal_attempt == expected_attempt
        assert manifest.failure_class == "product"
        assert manifest.terminal_cause == "worker_no_changes"
        assert not Path(manifest.git.worktree).exists()
        assert not git.branch_exists(manifest.git.branch)

    assert len(list((git.root / ".ai/runs").glob("*/manifest.json"))) == 3
    assert git.run_git("branch", "--list", "ai/*") == ""
    worktree_entries = git.run_git("worktree", "list", "--porcelain").splitlines()
    assert sum(line.startswith("worktree ") for line in worktree_entries) == 1
    blocked_adapter = NoOpWorker(script(review=BLOCKING))
    with pytest.raises(GoalError, match="3-attempt limit"):
        execute_run(
            git,
            CONFIG,
            registry(blocked_adapter),
            "add a flag",
            tmp_path / "wt",
            CRITERIA,
            goal_id=goal_id,
        )
    assert blocked_adapter.seen == []
    assert len(list((git.root / ".ai/runs").glob("*/manifest.json"))) == 3
    assert git.run_git("branch", "--list", "ai/*") == ""
    worktree_entries = git.run_git("worktree", "list", "--porcelain").splitlines()
    assert sum(line.startswith("worktree ") for line in worktree_entries) == 1

    assert GoalLedger(git.root).reopen(goal_id, "human approved a revised approach") == 1
    with GoalLedger(git.root).reserve(goal_id) as reservation:
        assert reservation.generation == 1
        assert reservation.attempt == 1


def test_scope_violation_escalates_for_uncommitted_out_of_scope_file(tmp_path):
    git = repo(tmp_path)
    goal_id = "scope-violation-goal"

    class Touching(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "vendor").mkdir()
                (request.cwd / "vendor" / "sneaky.py").write_text("x\n")
            return super().run(request)

    outcome = execute_run(
        git,
        CONFIG,
        registry(Touching(script())),
        "add a flag",
        tmp_path / "wt",
        CRITERIA,
        goal_id=goal_id,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "vendor/sneaky.py" in outcome.reason
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert manifest.terminal_cause == "scope_violation"
    assert not (outcome.store_root / "execution/no-progress/implementation-worker.json").exists()
    next_adapter = Scripted(script())
    with pytest.raises(GoalError, match="human decision"):
        execute_run(
            git,
            CONFIG,
            registry(next_adapter),
            "add a flag",
            tmp_path / "wt",
            CRITERIA,
            goal_id=goal_id,
        )
    assert next_adapter.seen == []


def test_plan_review_escalation_stops_the_goal_for_human_decision(tmp_path):
    git = repo(tmp_path)
    goal_id = "plan-review-goal"
    adapter = Scripted(script())
    adapter.script["plan_reviewer"] = [
        {
            "outcome": "ESCALATE",
            "findings": [
                {
                    "id": "F001",
                    "severity": "blocking",
                    "summary": "plan needs a human decision",
                    "evidence_required": "decision record",
                }
            ],
        }
    ]
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "add a flag",
        tmp_path / "wt",
        CRITERIA,
        goal_id=goal_id,
    )
    assert outcome.final_state is RunState.ESCALATED
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert manifest.terminal_cause == "plan_review_escalated"

    next_adapter = Scripted(script())
    with pytest.raises(GoalError, match="human decision"):
        execute_run(
            git,
            CONFIG,
            registry(next_adapter),
            "add a flag",
            tmp_path / "wt",
            CRITERIA,
            goal_id=goal_id,
        )
    assert next_adapter.seen == []


def test_success_and_escalation_leave_complete_audit_artifacts(tmp_path):
    git = repo(tmp_path)
    base = git.current_commit()
    success = execute_run(
        git, CONFIG, registry(Scripted(script(tier="trivial"))), "fix", tmp_path / "wt", CRITERIA
    )
    assert success.final_state is RunState.COMPLETE_LOCAL
    for relative in (
        "request.md",
        "classification.json",
        "context.json",
        "acceptance-criteria.json",
        "verification/final-verification.json",
        "execution/execution-summary.json",
    ):
        assert (success.store_root / relative).is_file(), relative
    manifest = RunManifest.model_validate_json(
        (success.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    summary = json.loads((success.store_root / "execution" / "execution-summary.json").read_text())
    verification = json.loads(
        (success.store_root / "verification" / "final-verification.json").read_text()
    )
    assert manifest.git.base_commit == base == summary["base_commit"]
    assert manifest.git.final_commit == summary["final_commit"] != base
    assert manifest.acceptance_criteria == CRITERIA
    assert [item["criterion"] for item in verification["verdicts"]] == CRITERIA
    assert any(event["event"] == "state_changed" for event in events(success))

    escalated = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate",
        tmp_path / "wt",
        CRITERIA,
    )
    assert escalated.final_state is RunState.ESCALATED
    for relative in ("request.md", "classification.json", "context.json", "manifest.json"):
        assert (escalated.store_root / relative).is_file(), relative
    escalated_manifest = RunManifest.model_validate_json(
        (escalated.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert escalated_manifest.terminal_reason
    assert escalated_manifest.git.final_commit is None
    assert escalated_manifest.completed_at is not None
    assert [event["event"] for event in events(escalated)][-1] in {
        "state_changed",
        "run_terminal",
        "run_cleanup_complete",
        "run_cleanup_blocked",
        "worktree_removed",
    }


def test_escalation_with_a_clean_worktree_removes_it(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate before writing anything",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert not worktree.exists()
    assert any(event["event"] == "worktree_removed" for event in events(outcome))


def test_escalation_archives_dirty_changes_then_removes_the_worktree_and_branch(tmp_path):
    git = repo(tmp_path)

    class CommittingWorker(Scripted):
        def run(self, request):
            result = super().run(request)
            if request.role == "implementation_worker":
                GitRepo(request.cwd).commit("worker checkpoint", ["src/app.py"])
                (request.cwd / "src" / "payload.bin").write_bytes(b"\x00\x01\xff")
                executable = request.cwd / "src" / "tool.sh"
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            return result

    outcome = execute_run(
        git,
        CONFIG,
        registry(CommittingWorker(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate after writing something",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert not worktree.exists()
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    saved = (outcome.store_root / "execution/failure-change.patch").read_bytes()
    assert b"x = 2" in saved
    assert b"GIT binary patch" in saved
    assert b"new file mode 100755" in saved
    assert git.verify_patch(manifest.git.base_commit, saved)
    restored = tmp_path / "restored-archive"
    git.run_git("worktree", "add", "--quiet", "--detach", str(restored), manifest.git.base_commit)
    restored_repo = GitRepo(restored)
    restored_repo.apply_patch_bytes(saved)
    assert (restored / "src" / "app.py").read_text() == "x = 2\n"
    assert (restored / "src" / "payload.bin").read_bytes() == b"\x00\x01\xff"
    assert (restored / "src" / "tool.sh").stat().st_mode & 0o111
    git.run_git("worktree", "remove", "--force", str(restored))
    bundle = outcome.store_root / "execution/failure-branch.bundle"
    assert bundle.is_file()
    receipt = json.loads((outcome.store_root / "execution/cleanup-receipt.json").read_text())
    heads = git.run_git("bundle", "list-heads", str(bundle))
    assert f"{receipt['branch_head']} refs/heads/{manifest.git.branch}" in heads
    assert any(event["event"] == "worktree_removed" for event in events(outcome))
    assert any(event["event"] == "run_cleanup_complete" for event in events(outcome))


def test_cleanup_failure_and_interruption_keep_the_run_reserved_until_finished(
    tmp_path, monkeypatch
):
    import dev_orchestration.workflow.runner as runner_module
    from dev_orchestration.workflow.bootstrap import bootstrap_run
    from dev_orchestration.workflow.engine import Engine

    git = repo(tmp_path)
    store, worktree = bootstrap_run(
        git, CONFIG, "cleanup recovery", Tier.STANDARD, tmp_path / "wt", "cleanup-recovery"
    )
    manifest = store.read_manifest()
    (worktree / "src" / "app.py").write_text("x = 2\n")
    engine = Engine(store)
    store.update_manifest(terminal_reason="fixture failure", terminal_cause="other")
    engine.transition(RunState.FAILED)

    verify_patch = GitRepo.verify_patch
    monkeypatch.setattr(GitRepo, "verify_patch", lambda self, base, patch: False)
    runner_module.cleanup_worktree_if_unproductive(engine, git)
    assert worktree.exists()
    assert git.branch_exists(manifest.git.branch)
    with pytest.raises(GoalError, match="finish its saved cleanup"):
        bootstrap_run(git, CONFIG, "duplicate", Tier.STANDARD, tmp_path / "wt", "cleanup-recovery")

    monkeypatch.setattr(GitRepo, "verify_patch", verify_patch)
    remove_worktree = runner_module.remove_worktree

    def interrupt_after_remove(repo_arg, path):
        remove_worktree(repo_arg, path)
        raise OSError("simulated interruption after worktree removal")

    monkeypatch.setattr(runner_module, "remove_worktree", interrupt_after_remove)
    runner_module.cleanup_worktree_if_unproductive(engine, git)
    assert not worktree.exists()
    assert git.branch_exists(manifest.git.branch)
    assert (store.root / "execution/cleanup-receipt.json").is_file()
    with pytest.raises(GoalError, match="finish its saved cleanup"):
        bootstrap_run(git, CONFIG, "duplicate", Tier.STANDARD, tmp_path / "wt", "cleanup-recovery")

    monkeypatch.setattr(runner_module, "remove_worktree", remove_worktree)
    runner_module.cleanup_worktree_if_unproductive(engine, git)
    assert not git.branch_exists(manifest.git.branch)
    recorded = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "run_cleanup_complete" for event in recorded)


def test_missing_post_bootstrap_worktree_without_archive_blocks_cleanup(tmp_path):
    import dev_orchestration.workflow.runner as runner_module
    from dev_orchestration.workflow.bootstrap import bootstrap_run
    from dev_orchestration.workflow.engine import Engine

    git = repo(tmp_path)
    store, worktree = bootstrap_run(
        git, CONFIG, "missing worktree", Tier.STANDARD, tmp_path / "wt", "missing-worktree"
    )
    manifest = store.read_manifest()
    (worktree / "src" / "app.py").write_text("unarchived change\n")
    engine = Engine(store)
    store.update_manifest(terminal_reason="fixture failure", terminal_cause="other")
    engine.transition(RunState.FAILED)
    git.run_git("worktree", "remove", "--force", str(worktree))

    runner_module.cleanup_worktree_if_unproductive(engine, git)

    assert not worktree.exists()
    assert git.branch_exists(manifest.git.branch)
    assert not (store.root / "execution/failure-change.patch").exists()
    recorded = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "run_cleanup_blocked" for event in recorded)
    assert not any(event["event"] == "run_cleanup_complete" for event in recorded)
    with pytest.raises(GoalError, match="finish its saved cleanup"):
        bootstrap_run(git, CONFIG, "duplicate", Tier.STANDARD, tmp_path / "wt", "missing-worktree")


def test_cleanup_rechecks_for_external_edits_before_removal(tmp_path, monkeypatch):
    import dev_orchestration.workflow.runner as runner_module
    from dev_orchestration.workflow.bootstrap import bootstrap_run
    from dev_orchestration.workflow.engine import Engine

    git = repo(tmp_path)
    store, worktree = bootstrap_run(
        git, CONFIG, "external edit guard", Tier.STANDARD, tmp_path / "wt", "edit-guard"
    )
    manifest = store.read_manifest()
    (worktree / "src" / "app.py").write_text("x = 2\n")
    engine = Engine(store)
    store.update_manifest(terminal_reason="fixture failure", terminal_cause="other")
    engine.transition(RunState.FAILED)

    verify_receipt = runner_module._verify_cleanup_receipt
    external_edit = worktree / "src" / "external.py"

    def verify_then_edit(engine_arg, repo_arg, receipt):
        verify_receipt(engine_arg, repo_arg, receipt)
        external_edit.write_text("retain this unexpected edit\n")

    monkeypatch.setattr(runner_module, "_verify_cleanup_receipt", verify_then_edit)
    runner_module.cleanup_worktree_if_unproductive(engine, git)
    assert worktree.exists()
    assert external_edit.read_text() == "retain this unexpected edit\n"
    assert git.branch_exists(manifest.git.branch)
    recorded = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert not any(event["event"] == "run_cleanup_complete" for event in recorded)
    with pytest.raises(GoalError, match="finish its saved cleanup"):
        bootstrap_run(git, CONFIG, "duplicate", Tier.STANDARD, tmp_path / "wt", "edit-guard")

    monkeypatch.setattr(runner_module, "_verify_cleanup_receipt", verify_receipt)
    external_edit.unlink()
    runner_module.cleanup_worktree_if_unproductive(engine, git)
    assert not worktree.exists()
    assert not git.branch_exists(manifest.git.branch)


def test_completed_run_keeps_its_worktree_for_human_review(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(tier="trivial"))), "fix", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert worktree.exists()
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert git.branch_exists(manifest.git.branch)
    assert not any(
        event["event"].startswith("worktree_cleanup") or event["event"] == "worktree_removed"
        for event in events(outcome)
    )


def test_external_plan_is_imported_hashed_and_planner_is_skipped(tmp_path):
    git = repo(tmp_path)
    source = tmp_path / "external.md"
    source.write_text("# external plan\n")
    adapter = Scripted(script())
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "add a flag",
        tmp_path / "wt",
        CRITERIA,
        external_plan=source,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "planner" not in adapter.seen
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["plan_origin"] == "external"
    assert manifest["plan_sha256"]
    assert (outcome.store_root / "planning/plan-review-receipt.json").is_file()
    assert (
        "planning/plan-review-receipt.json"
        in json.loads((outcome.store_root / manifest["checkpoint"]).read_text())["artifacts"]
    )


def test_matching_reviewed_plan_reuses_review_without_provider_calls(tmp_path):
    git = repo(tmp_path)
    plan_source = tmp_path / "external.md"
    plan_source.write_text(PLAN)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(script())),
        "same request",
        tmp_path / "wt1",
        CRITERIA,
        external_plan=plan_source,
    )
    source = RunStore(git.root, first.run_id)
    adapter = Scripted(script())
    second = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "same request",
        tmp_path / "wt2",
        CRITERIA,
        external_plan=source.root / "planning/approved-plan.md",
        reviewed_plan_from=source,
    )
    assert second.final_state is RunState.COMPLETE_LOCAL
    assert not {"planner", "plan_reviewer", "plan_reconciler"} & set(adapter.seen)
    manifest = json.loads((second.store_root / "manifest.json").read_text())
    assert manifest["plan_review_provenance"]["source_receipt"] == json.loads(
        (source.root / "planning/plan-review-receipt.json").read_text()
    )


def test_same_base_retry_automatically_reuses_completed_plan_review(tmp_path):
    git = repo(tmp_path)
    plan_source = tmp_path / "external.md"
    plan_source.write_text(PLAN)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(script())),
        "same request",
        tmp_path / "wt1",
        CRITERIA,
        external_plan=plan_source,
    )
    source = RunStore(git.root, first.run_id)
    source_receipt = json.loads((source.root / "planning/plan-review-receipt.json").read_text())
    adapter = Scripted(script())

    retried = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "same request",
        tmp_path / "wt2",
        CRITERIA,
        external_plan=source.root / "planning/approved-plan.md",
        reviewed_plan_from=source,
        goal_id="separate-request",
    )

    assert retried.final_state is RunState.COMPLETE_LOCAL
    assert not {"planner", "plan_reviewer", "plan_reconciler"} & set(adapter.seen)
    manifest = json.loads((retried.store_root / "manifest.json").read_text())
    assert manifest["plan_review_provenance"]["source_receipt"] == source_receipt
    assert "plan_review_reused" in {event["event"] for event in events(retried)}


def test_reused_plan_review_seeds_finding_ids_for_new_reviews(tmp_path):
    git = repo(tmp_path)
    plan_source = tmp_path / "external.md"
    plan_source.write_text(PLAN)
    plan_finding = {
        "outcome": "PASS",
        "findings": [
            {"id": "x", "severity": "minor", "summary": "plan note", "file": "src/app.py"}
        ],
    }
    source_adapter = Scripted(script())
    source_adapter.script["plan_reviewer"] = [plan_finding]
    first = execute_run(
        git,
        CONFIG,
        registry(source_adapter),
        "same request",
        tmp_path / "source",
        CRITERIA,
        external_plan=plan_source,
        goal_id="explicit-review-recovery",
    )
    source = RunStore(git.root, first.run_id)
    implementation_finding = {
        "outcome": "PASS",
        "findings": [
            {
                "id": "x",
                "severity": "minor",
                "summary": "implementation note",
                "file": "src/app.py",
            }
        ],
    }
    noted = dict(
        VERIFY,
        finding_dispositions=[{"finding_id": "F002", "status": "unresolved", "evidence": "note"}],
    )
    adapter = Scripted(script(review=implementation_finding, verifier=noted))
    second = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "same request",
        tmp_path / "reused",
        CRITERIA,
        external_plan=source.root / "planning/approved-plan.md",
        reviewed_plan_from=source,
    )

    assert second.final_state is RunState.COMPLETE_LOCAL
    plan_review = json.loads((second.store_root / "review/plan-review-v1.json").read_text())
    implementation_review = json.loads(
        (second.store_root / "review/implementation-review-v1.json").read_text()
    )
    assert plan_review["findings"][0]["id"] == "F001"
    assert implementation_review["findings"][0]["id"] == "F002"


def test_invalid_review_source_does_not_bootstrap_a_new_run(tmp_path):
    git = repo(tmp_path)
    plan_source = tmp_path / "external.md"
    plan_source.write_text(PLAN)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(script())),
        "same request",
        tmp_path / "source",
        CRITERIA,
        external_plan=plan_source,
    )
    source = RunStore(git.root, first.run_id)
    receipt = source.root / "planning/plan-review-receipt.json"
    receipt.write_text(receipt.read_text() + " ")

    with pytest.raises(CheckpointError, match="changed or is missing"):
        execute_run(
            git,
            CONFIG,
            registry(Scripted(script())),
            "same request",
            tmp_path / "reused",
            CRITERIA,
            external_plan=source.root / "planning/approved-plan.md",
            reviewed_plan_from=source,
        )

    assert len(list((git.root / ".ai" / "runs").glob("*/manifest.json"))) == 1
    assert not (tmp_path / "reused" / "demo").exists()


def test_run_that_changes_nothing_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    adapter = NoOpWorker(script())
    outcome = execute_run(git, CONFIG, registry(adapter), "add a flag", tmp_path / "wt", CRITERIA)
    assert outcome.final_state is RunState.FAILED
    assert "no deliverable changes" in outcome.reason
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["git"]["final_commit"] is None
    assert manifest["terminal_cause"] == "worker_no_changes"
    assert manifest["failure_class"] == "product"
    assert manifest["failure_signature"] == "worker_no_changes:empty-change-set"
    assert adapter.seen == ["classifier", "planner", "plan_reviewer", "implementation_worker"]
    evidence = json.loads(
        (outcome.store_root / "execution/no-progress/implementation-worker.json").read_text()
    )
    assert evidence["stage"] == "implementation_worker"
    assert evidence["comparison"] == "empty_change_patch"
    assert evidence["comparison_format"] == "git-change-patch-bytes"
    assert evidence["after_bytes"] == 0


def test_staged_edit_restored_to_base_is_empty_worker_output(tmp_path):
    class StageThenRestore(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                app = request.cwd / "src" / "app.py"
                app.write_text("x = 2\n")
                subprocess.run(["git", "-C", str(request.cwd), "add", "src/app.py"], check=True)
                app.write_text("x = 1\n")
                now = datetime.now(UTC)
                return AgentResult("fake", None, 0, "restored", now, now)
            return super().run(request)

    git = repo(tmp_path)
    adapter = StageThenRestore(script())
    outcome = execute_run(
        git, CONFIG, registry(adapter), "try and restore an edit", tmp_path / "wt", CRITERIA
    )

    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert outcome.final_state is RunState.FAILED
    assert manifest.terminal_cause == "worker_no_changes"
    assert adapter.seen == ["classifier", "planner", "plan_reviewer", "implementation_worker"]
    assert not (outcome.store_root / "execution/validation-v1.json").exists()


def test_seed_patch_counts_as_deliverable_when_worker_is_a_noop(tmp_path):
    git = repo(tmp_path)
    seed = tmp_path / "seed.patch"
    seed.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 99\n"
    )

    adapter = NoOpWorker(script())
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "continue a seeded change",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=seed,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[-2:] == ["implementation_reviewer", "verifier"]
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["git"]["final_commit"] is not None
    assert not (outcome.store_root / "execution/no-progress/implementation-worker.json").exists()


@pytest.mark.parametrize("change_kind", ["binary", "deletion", "mode", "empty_untracked"])
def test_binary_deletion_mode_and_empty_untracked_changes_are_deliverable(tmp_path, change_kind):
    class DeliverableWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                self.requests.append(request)
                if change_kind == "binary":
                    (request.cwd / "src" / "blob.bin").write_bytes(b"\x00\xff")
                elif change_kind == "deletion":
                    (request.cwd / "src" / "app.py").unlink()
                elif change_kind == "mode":
                    (request.cwd / "src" / "app.py").chmod(0o755)
                else:
                    (request.cwd / "src" / "empty.txt").write_bytes(b"")
                now = datetime.now(UTC)
                return AgentResult("fake", None, 0, "done", now, now)
            return super().run(request)

    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(DeliverableWorker(script())),
        f"make a {change_kind} change",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert not (outcome.store_root / "execution/no-progress/implementation-worker.json").exists()


def test_verifier_that_judges_no_criteria_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    empty = {"outcome": "PASS", "verdicts": []}
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(verifier=empty))),
        "do it",
        tmp_path / "wt",
        CRITERIA,
    )
    # The answer is retried once, then the run escalates: it never judged AC1.
    assert outcome.final_state is RunState.ESCALATED
    assert "missing=['AC1']" in outcome.reason


def test_verifier_that_names_a_criterion_it_was_not_given_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    swapped = {
        "outcome": "PASS",
        "verdicts": [{"criterion_id": "AC2", "verdict": "PASS", "evidence": "sure"}],
    }
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(verifier=swapped))),
        "do it",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "unknown=['AC2']" in outcome.reason


def test_a_run_without_acceptance_criteria_is_refused(tmp_path):
    git = repo(tmp_path)
    with pytest.raises(ContextContractError):
        execute_run(git, CONFIG, registry(Scripted(script())), "do it", tmp_path / "wt", [])


@pytest.mark.parametrize("criteria", [["   "], ["done", " done "]])
def test_blank_or_duplicate_acceptance_criteria_are_refused(tmp_path, criteria):
    git = repo(tmp_path)
    with pytest.raises(ContextContractError):
        execute_run(git, CONFIG, registry(Scripted(script())), "do it", tmp_path / "wt", criteria)


def test_override_may_raise_the_tier(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="trivial"))),
        "fix",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.STANDARD,
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "standard"


def test_override_may_not_lower_the_tier_below_the_classification(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="standard"))),
        "rewrite auth",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.TRIVIAL,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "trivial" in outcome.reason and "standard" in outcome.reason


def test_downgrade_is_allowed_when_a_reason_is_recorded(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="standard"))),
        "document the auth boundary",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.TRIVIAL,
        downgrade_reason="documentation only; classifier judged the subject, not the change",
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "trivial"
    assert "documentation only" in manifest["downgrade_reason"]
    resolved = [event for event in events(outcome) if event["event"] == "tier_resolved"]
    assert resolved and resolved[0]["downgraded"] is True
    assert "documentation only" in resolved[0]["downgrade_reason"]


def test_blank_downgrade_reason_does_not_authorize_a_downgrade(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="standard"))),
        "rewrite auth",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.TRIVIAL,
        downgrade_reason="   ",
    )
    assert outcome.final_state is RunState.ESCALATED


def test_a_reason_does_not_change_an_upgrade(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="trivial"))),
        "fix",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.STANDARD,
        downgrade_reason="ignored on an upgrade",
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "standard"


def test_trivial_run_skips_implementation_review(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(tier="trivial"))), "fix", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    names = [event["event"] for event in events(outcome)]
    assert "implementation_review_skipped" in names
    assert "implementation_reviewed" not in names
    assert "verified" in names


def test_ignored_out_of_scope_write_escalates_the_run(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\ndist/\n")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore dist"], check=True)

    class Sneaky(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "dist").mkdir(exist_ok=True)
                (request.cwd / "dist" / "payload.sh").write_text("#!/bin/sh\n")
            return super().run(request)

    outcome = execute_run(
        git, CONFIG, registry(Sneaky(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "dist/payload.sh" in outcome.reason


UNVALIDATED_TRIVIAL_CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "demo", "class": "internal_utility"},
        "scope": {"include": ["src/"], "exclude": []},
        "context": {"persistent": []},
        "validation": {"unit": {"command": "true", "required_for": ["standard"]}},
    }
)


def test_trivial_keeps_review_when_no_validation_runs_at_that_tier(tmp_path):
    """Dropping review assumes validation covers it; without validation it must stay."""
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        UNVALIDATED_TRIVIAL_CONFIG,
        registry(Scripted(script(tier="trivial"))),
        "fix",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    names = [event["event"] for event in events(outcome)]
    assert "implementation_reviewed" in names
    assert "implementation_review_skipped" not in names


def test_validation_repair_runs_for_continued_patch(tmp_path):
    class RepairingWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                self.requests.append(request)
                (request.cwd / "src" / "app.py").write_text("x = 2\n")
                now = datetime.now(UTC)
                return AgentResult("fake", None, 0, "repaired", now, now)
            return super().run(request)

    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": "test \"$(cat src/app.py)\" = 'x = 2'",
                    "required_for": ["standard"],
                }
            }
        }
    )
    plan_path = tmp_path / "approved-plan.md"
    plan_path.write_text("# Approved plan\n")
    patch_path = tmp_path / "saved.patch"
    patch_path.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 0\n"
    )
    adapter = RepairingWorker(script())
    outcome = execute_run(
        git,
        config,
        registry(adapter),
        "continue the saved change",
        tmp_path / "wt",
        CRITERIA,
        external_plan=plan_path,
        retry_of="20260930-010000_standard_source",
        continue_patch=patch_path,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen.count("implementation_worker") == 1
    assert "planner" not in adapter.seen
    assert "change_continued" in {event["event"] for event in events(outcome)}
    assert (outcome.store_root / "execution/validation-repair-v1.json").is_file()
    assert adapter.seen[-2:] == ["implementation_reviewer", "verifier"]


def test_continued_patch_counts_as_deliverable_without_new_worker_changes(tmp_path):
    git = repo(tmp_path)
    plan_path = tmp_path / "approved-plan.md"
    plan_path.write_text("# Approved plan\n")
    patch_path = tmp_path / "saved.patch"
    patch_path.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 3\n"
    )
    adapter = NoOpWorker(script())

    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "continue the saved change",
        tmp_path / "wt",
        CRITERIA,
        external_plan=plan_path,
        retry_of="source-run",
        continue_patch=patch_path,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert "implementation_worker" not in adapter.seen
    assert "change_continued" in {event["event"] for event in events(outcome)}
    assert not (outcome.store_root / "execution/no-progress/implementation-worker.json").exists()


def test_trivial_run_repairs_validation_without_adding_implementation_review(tmp_path):
    class RepairingWorker(Scripted):
        worker_calls = 0

        def run(self, request):
            result = super().run(request)
            if request.role == "implementation_worker":
                self.worker_calls += 1
                value = "x = 1\n" if self.worker_calls == 1 else "x = 2\n"
                (request.cwd / "src" / "app.py").write_text(value)
                if self.worker_calls == 1:
                    (request.cwd / "src" / "initial-attempt.txt").write_text("attempted\n")
            return result

    git = repo(tmp_path)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": "test \"$(cat src/app.py)\" = 'x = 2'",
                    "required_for": ["trivial"],
                }
            }
        }
    )
    adapter = RepairingWorker(script(tier="trivial"))
    outcome = execute_run(
        git, config, registry(adapter), "repair the flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    assert "implementation_reviewer" not in adapter.seen
    assert adapter.seen[-1] == "verifier"
    assert (outcome.store_root / "execution/validation-v2.json").is_file()


@pytest.mark.parametrize(
    "review",
    [
        {"outcome": "ESCALATE", "findings": []},
        {
            "outcome": "ESCALATE",
            "findings": [
                {
                    "id": "F001",
                    "severity": "blocking",
                    "summary": "needs human decision",
                    "evidence_required": "resolution of review boundary",
                }
            ],
        },
    ],
)
def test_direct_implementation_escalation_is_retryable_and_skips_verifier(tmp_path, review):
    git = repo(tmp_path)
    adapter = Scripted(script(review=review))
    outcome = execute_run(git, CONFIG, registry(adapter), "change app", tmp_path / "wt", CRITERIA)
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert outcome.final_state is RunState.ESCALATED
    assert manifest.terminal_cause == "implementation_review_escalated"
    assert "verifier" not in adapter.seen
