import json
import subprocess
from datetime import UTC, datetime

import pytest

from dev_orchestration.adapters.base import AdapterStatus, AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.config.models import ProjectConfig, RoleConfig
from dev_orchestration.context.assembler import ContextContractError
from dev_orchestration.domain.enums import RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo
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


def registry(adapter):
    return RoleRegistry(
        roles={role: RoleConfig(adapter="fake", model="m") for role in ROLES},
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
    }


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
