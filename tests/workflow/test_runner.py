import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AdapterStatus, AgentResult, AgentTimeoutError
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig, RoleConfig
from dev_orchestration.context.assembler import ContextContractError
from dev_orchestration.domain.enums import RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.cloud import link_cloud_session
from dev_orchestration.workflow.recovery import ResumeRefused, approve_run, resume_run
from dev_orchestration.workflow.runner import execute_run

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
    "criteria": CRITERIA,
    "verdicts": [{"criterion": CRITERIA[0], "verdict": "PASS", "evidence": "seen"}],
    "unresolved_finding_ids": [],
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
        git, config, registry(adapter), "change the flag with approval", tmp_path / "wt", CRITERIA
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
    outcome = execute_run(
        git, config, registry(Scripted(script())), "bad", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.FAILED
    assert "validation" in outcome.reason
    data = json.loads(
        next((outcome.store_root / "execution").glob("validation-v1.json")).read_text()
    )
    assert data[0]["exit_code"] == 3


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
    outcome = execute_run(
        git,
        CONFIG,
        registry(TimeoutWorker(script()), worker_timeout=7),
        "timeout safely",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.PAUSED_INTERRUPTED
    pause = json.loads((outcome.store_root / "manifest.json").read_text())["pause"]
    assert pause["role"] == "implementation_worker"
    assert "7-second" in pause["diagnostic"]


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_fail_or_unknown_verification_cannot_complete_local(tmp_path, verdict):
    git = repo(tmp_path)
    failed = dict(
        VERIFY,
        outcome="CHANGES_REQUIRED",
        verdicts=[{"criterion": CRITERIA[0], "verdict": verdict, "evidence": "no"}],
    )
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(verifier=failed))), "bad", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.FAILED


def test_blocked_escalated_and_nonzero_agent_results_are_terminal(tmp_path):
    git = repo(tmp_path)
    blocked = dict(PASS, outcome="BLOCKED")
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(review=blocked))), "bad", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.BLOCKED
    escalated = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "bad",
        tmp_path / "wt",
        CRITERIA,
    )
    assert escalated.final_state is RunState.ESCALATED
    nonzero = execute_run(
        git,
        CONFIG,
        registry(NonzeroWorker(script())),
        "bad",
        tmp_path / "wt",
        CRITERIA,
    )
    assert nonzero.final_state is RunState.ESCALATED


def test_scope_violation_escalates_for_uncommitted_out_of_scope_file(tmp_path):
    git = repo(tmp_path)

    class Touching(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "vendor").mkdir()
                (request.cwd / "vendor" / "sneaky.py").write_text("x\n")
            return super().run(request)

    outcome = execute_run(
        git, CONFIG, registry(Touching(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "vendor/sneaky.py" in outcome.reason


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
        "worktree_cleanup_skipped",
        "worktree_cleanup_failed",
        "worktree_removed",
    }


def test_escalation_with_a_clean_worktree_removes_it(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(NoOpWorker(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate before writing anything",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert not worktree.exists()
    assert any(event["event"] == "worktree_removed" for event in events(outcome))


def test_escalation_with_a_dirty_worktree_leaves_it_for_manual_review(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate after writing something",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert worktree.exists()
    assert (worktree / "src" / "app.py").read_text() == "x = 2\n"
    assert any(event["event"] == "worktree_cleanup_skipped" for event in events(outcome))


def test_completed_run_keeps_its_worktree_for_human_review(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(tier="trivial"))), "fix", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert worktree.exists()
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


def test_run_that_changes_nothing_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(NoOpWorker(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.FAILED
    assert "no change" in outcome.reason
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["git"]["final_commit"] is None


def test_verifier_that_judges_no_criteria_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    empty = {"outcome": "PASS", "criteria": [], "verdicts": [], "unresolved_finding_ids": []}
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(verifier=empty))),
        "do it",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.FAILED
    assert "criteria" in outcome.reason


def test_verifier_that_swaps_in_its_own_criteria_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    swapped = {
        "outcome": "PASS",
        "criteria": ["something easier"],
        "verdicts": [{"criterion": "something easier", "verdict": "PASS", "evidence": "sure"}],
        "unresolved_finding_ids": [],
    }
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(verifier=swapped))),
        "do it",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.FAILED


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
