"""Phase 2: acceptance contract, earlier checks, and in-place repair budgets."""

import hashlib
import itertools
import json
from datetime import UTC, datetime

import pytest

from dev_orchestration.adapters.base import AgentResult, AgentTimeoutError
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.domain.enums import RunState
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.workflow import runner as runner_module
from dev_orchestration.workflow.recovery import ResumeRefused, resume_run
from dev_orchestration.workflow.scope_check import ScopeViolation
from tests.workflow import test_resume
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_runner import (
    CONFIG,
    CRITERIA,
    PASS,
    VERIFY,
    Scripted,
    registry,
    repo,
    script,
)

CONTRACT = "planning/acceptance-contract.json"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_standard_run_saves_a_contract_bound_to_the_approved_plan(tmp_path):
    git = repo(tmp_path)
    adapter = Scripted(script())
    outcome = execute_run(git, CONFIG, registry(adapter), "add a flag", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    contract = json.loads((outcome.store_root / CONTRACT).read_text())
    assert contract["plan_sha256"] == _sha(outcome.store_root / "planning/approved-plan.md")
    assert contract["checklist"] == CRITERIA
    assert [item["name"] for item in contract["validation"]] == ["unit"]
    assert contract["file_scope"] == {"include": ["src/"], "exclude": []}


def test_trivial_run_binds_the_contract_to_the_task_contract(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="trivial"))),
        "add a flag",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    contract = json.loads((outcome.store_root / CONTRACT).read_text())
    assert contract["plan_sha256"] == _sha(outcome.store_root / "planning/task-contract.md")


def test_worker_prompt_places_the_contract_before_changing_context(tmp_path):
    git = repo(tmp_path)
    adapter = Scripted(script())
    execute_run(git, CONFIG, registry(adapter), "add a flag", tmp_path / "wt", CRITERIA)

    worker = next(r for r in adapter.requests if r.role == "implementation_worker")
    labels = worker.context.labels()
    assert labels.index("acceptance_contract") == labels.index("approved_plan") + 1
    assert labels.index("acceptance_contract") < labels.index("worktree")


def test_worker_role_requires_a_self_check():
    assert "self-check" in load_role_prompt("implementation_worker").lower()


class Recording(test_resume.Scripted):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return super().run(request)


def _worker_contract(adapter):
    worker = next(r for r in adapter.requests if r.role == "implementation_worker")
    return [item.content for item in worker.context.items if item.label == "acceptance_contract"]


def test_resume_reuses_the_saved_contract_unchanged(tmp_path):
    git = test_resume.repo(tmp_path)
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    saved = (first.store_root / CONTRACT).read_bytes()

    adapter = Recording()
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert (first.store_root / CONTRACT).read_bytes() == saved
    assert _worker_contract(adapter) == [json.dumps(json.loads(saved), indent=2, sort_keys=True)]


def test_resume_of_a_run_saved_without_a_contract_keeps_the_older_path(tmp_path, monkeypatch):
    git = test_resume.repo(tmp_path)
    monkeypatch.setattr(runner_module, "save_contract", lambda *args, **kwargs: CONTRACT)
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )
    assert not (first.store_root / CONTRACT).exists()

    adapter = Recording()
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert _worker_contract(adapter) == []
    assert not (first.store_root / CONTRACT).exists()


def test_resume_refuses_a_contract_that_no_longer_matches_the_plan(tmp_path):
    git = test_resume.repo(tmp_path)
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )
    path = first.store_root / CONTRACT
    path.write_text(path.read_text().replace(json.loads(path.read_text())["plan_sha256"], "0" * 64))

    with pytest.raises(ResumeRefused, match="acceptance-contract.json changed"):
        resume_run(git, test_resume.CONFIG, test_resume.registry(Recording()), first.run_id)


# --- earlier checks and independent repair budgets (new runs) ---------------------------


class Varying(Scripted):
    """A worker whose every call leaves different bytes, so no repair is 'no progress'."""

    def __init__(self, scripts, *, extra=None):
        super().__init__(scripts)
        self.worker_calls = 0
        self.extra = extra

    def run(self, request):
        if request.role == "implementation_worker":
            self.worker_calls += 1
            self.seen.append(request.role)
            self.requests.append(request)
            (request.cwd / "src" / "app.py").write_text(f"x = {self.worker_calls + 1}\n")
            if self.extra:
                self.extra(request, self.worker_calls)
            now = datetime.now(UTC)
            return AgentResult("fake", None, 0, "done", now, now)
        return super().run(request)


def _verdict(verdict, criteria=CRITERIA):
    return dict(
        VERIFY,
        outcome="CHANGES_REQUIRED" if verdict != "PASS" else "PASS",
        criteria=criteria,
        verdicts=[{"criterion": criteria[0], "verdict": verdict, "evidence": "observed"}],
    )


def _config(command):
    data = CONFIG.model_dump(mode="json")
    data["validation"] = {"unit": {"command": command, "required_for": ["standard"]}}
    return ProjectConfig.model_validate(data)


def _manifest(outcome):
    return RunStore(outcome.store_root.parents[2], outcome.run_id).read_manifest()


def _labels(request, label):
    return [item.content for item in request.context.items if item.label == label]


def test_checklist_naming_a_missing_file_goes_to_repair_before_any_review(tmp_path):
    criteria = ["`src/new_module.py` exists"]

    def create_on_repair(request, call):
        if call == 2:
            (request.cwd / "src" / "new_module.py").write_text("y = 1\n")

    git = repo(tmp_path)
    adapter = Varying(script(verifier=_verdict("PASS", criteria)), extra=create_on_repair)
    outcome = execute_run(git, CONFIG, registry(adapter), "add module", tmp_path / "wt", criteria)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    first_review = adapter.seen.index("implementation_reviewer")
    assert (
        adapter.seen[:first_review]
        == ["classifier", "planner", "plan_reviewer"] + ["implementation_worker"] * 2
    )
    worker_requests = [r for r in adapter.requests if r.role == "implementation_worker"]
    assert "acceptance-precheck" in _labels(worker_requests[1], "validation_evidence")[0]
    assert _manifest(outcome).validation_repairs_reserved == 1


def test_validation_gets_two_repair_rounds(tmp_path):
    git = repo(tmp_path)
    adapter = Varying(script())
    outcome = execute_run(
        git,
        _config("test \"$(cat src/app.py)\" = 'x = 4'"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert _manifest(outcome).validation_repairs_reserved == 2
    assert (outcome.store_root / "execution/validation-repair-v2.json").is_file()


def test_validation_that_still_fails_after_two_repairs_stops(tmp_path):
    git = repo(tmp_path)
    adapter = Varying(script())
    outcome = execute_run(
        git,
        _config("test \"$(cat src/app.py)\" = 'x = 99'"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.FAILED
    assert adapter.worker_calls == 3
    assert _manifest(outcome).terminal_cause == "validation_failed"


def test_final_verification_rejection_is_repaired_in_place(tmp_path):
    git = repo(tmp_path)
    scripts = script() | {"verifier": [_verdict("FAIL"), _verdict("PASS")]}
    adapter = Varying(scripts)
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen.count("planner") == 1
    assert adapter.worker_calls == 2
    assert adapter.seen.count("implementation_reviewer") == 2
    assert adapter.seen.count("verifier") == 2
    worker_requests = [r for r in adapter.requests if r.role == "implementation_worker"]
    scope = json.loads(_labels(worker_requests[1], "repair_scope")[0])
    assert scope["gate"] == "final_verification"
    manifest = _manifest(outcome)
    assert manifest.verification_repairs_reserved == 1
    assert manifest.repair_rounds["final_verification"] == 1
    assert (outcome.store_root / "execution/repair-final_verification-v1.json").is_file()


def test_verification_escalates_after_two_failed_repairs(tmp_path):
    git = repo(tmp_path)
    adapter = Varying(script() | {"verifier": [_verdict("FAIL")]})
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 3
    manifest = _manifest(outcome)
    assert manifest.stop.repairs["final_verification"].model_dump() == {
        "used": 2,
        "allowed": 2,
        "exhausted": True,
    }
    assert manifest.terminal_cause == "verification_exhausted"


def test_verifier_escalation_is_not_repaired(tmp_path):
    git = repo(tmp_path)
    escalate = dict(_verdict("FAIL"), outcome="ESCALATE")
    adapter = Varying(script() | {"verifier": [escalate]})
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1


def test_review_rejection_naming_only_out_of_scope_files_escalates(tmp_path):
    finding = {
        "outcome": "CHANGES_REQUIRED",
        "findings": [
            {
                "id": "x",
                "severity": "blocking",
                "summary": "wrong place",
                "evidence_required": "fix",
                "file": "docs/outside.md",
            }
        ],
    }
    git = repo(tmp_path)
    adapter = Varying(script(review=finding))
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1
    assert _manifest(outcome).terminal_cause == "repair_scope_unsafe"


def test_repair_that_edits_beyond_the_named_files_is_stopped(tmp_path):
    finding = {
        "outcome": "CHANGES_REQUIRED",
        "findings": [
            {
                "id": "x",
                "severity": "blocking",
                "summary": "fix app",
                "evidence_required": "fix",
                "file": "src/app.py",
            }
        ],
    }

    def stray(request, call):
        if call == 2:
            (request.cwd / "src" / "stray.py").write_text("z = 1\n")

    git = repo(tmp_path)
    adapter = Varying(script(review=finding), extra=stray)
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.ESCALATED
    assert _manifest(outcome).terminal_cause == "scope_violation"
    assert "stray.py" in (_manifest(outcome).terminal_reason or "")


def test_review_budget_is_independent_of_a_used_verification_repair(tmp_path):
    blocking = {
        "outcome": "CHANGES_REQUIRED",
        "findings": [{"id": "x", "severity": "blocking", "summary": "s", "evidence_required": "e"}],
    }
    git = repo(tmp_path)
    scripts = script() | {
        "verifier": [_verdict("FAIL"), _verdict("PASS")],
        "implementation_reviewer": [PASS, blocking, PASS],
    }
    adapter = Varying(scripts)
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 3
    manifest = _manifest(outcome)
    assert manifest.repair_rounds == {"final_verification": 1, "implementation_review": 1}


# --- resume ---------------------------------------------------------------------------


_VALUES = itertools.count(100)


class Seq(Recording):
    """Resume-side adapter: scripted verifier verdicts, a unique write per worker call."""

    def __init__(
        self, *, verdicts=(), criteria=None, timeout_on_call=None, extra=None, review=None
    ):
        super().__init__()
        self.verdicts = list(verdicts)
        self.criteria = criteria or test_resume.CRITERIA
        self.timeout_on_call = timeout_on_call
        self.extra = extra
        self.review = review
        self.worker_calls = 0

    def run(self, request):
        if request.role == "implementation_worker":
            self.worker_calls += 1
            self.seen.append(request.role)
            self.requests.append(request)
            if self.timeout_on_call == self.worker_calls:
                raise AgentTimeoutError(request.role, 7, self.name)
            (request.cwd / "src" / "app.py").write_text(f"x = {next(_VALUES)}\n")
            if self.extra:
                self.extra(request, self.worker_calls)
            now = datetime.now(UTC)
            return AgentResult(self.name, None, 0, "done", now, now)
        if request.role == "verifier":
            self.seen.append(request.role)
            self.requests.append(request)
            verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            now = datetime.now(UTC)
            return AgentResult(self.name, None, 0, _verdict(verdict, self.criteria), now, now)
        if request.role == "implementation_reviewer" and self.review:
            self.seen.append(request.role)
            self.requests.append(request)
            now = datetime.now(UTC)
            return AgentResult(self.name, None, 0, self.review, now, now)
        return super().run(request)


def _first_run(git, tmp_path, adapter, criteria=None, config=None):
    return execute_run(
        git,
        config or test_resume.CONFIG,
        test_resume.registry(adapter),
        "change app",
        tmp_path / "wt",
        criteria or test_resume.CRITERIA,
    )


def _store(git, outcome):
    return RunStore(git.root, outcome.run_id)


def test_interrupted_verification_repair_resumes_the_reserved_round(tmp_path):
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(verdicts=["FAIL"], timeout_on_call=2))
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = _store(git, first)
    assert store.latest_checkpoint()["next_stage"] == "verification_repair"
    assert store.read_manifest().verification_repairs_reserved == 1

    adapter = Seq(verdicts=["PASS"])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_worker", "implementation_reviewer", "verifier"]
    manifest = store.read_manifest()
    assert manifest.verification_repairs_reserved == 1
    assert manifest.repair_rounds["final_verification"] == 1
    scope = json.loads(_labels(adapter.requests[0], "repair_scope")[0])
    assert (scope["gate"], scope["round"]) == ("final_verification", 1)


def test_resumed_final_verification_rejection_is_repaired_in_place(tmp_path):
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1))
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    adapter = Seq(verdicts=["FAIL", "PASS"])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_worker", "implementation_reviewer", "verifier"] * 2
    assert _store(git, first).read_manifest().repair_rounds["final_verification"] == 1


def test_resumed_verification_escalates_after_two_failed_repairs(tmp_path):
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1))

    adapter = Seq(verdicts=["FAIL"])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 3
    manifest = _store(git, first).read_manifest()
    assert manifest.verification_repairs_reserved == 2
    assert manifest.terminal_cause == "verification_exhausted"


def test_resumed_checklist_precheck_failure_goes_to_repair(tmp_path):
    criteria = ["`src/new_module.py` exists"]

    def create_on_repair(request, call):
        if call == 2:
            (request.cwd / "src" / "new_module.py").write_text("y = 1\n")

    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1, criteria=criteria), criteria)
    adapter = Seq(verdicts=["PASS"], criteria=criteria, extra=create_on_repair)
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == [
        "implementation_worker",
        "implementation_worker",
        "implementation_reviewer",
        "verifier",
    ]


def test_resume_of_a_checkpoint_without_a_saved_repair_description(tmp_path, monkeypatch):
    config = ProjectConfig.model_validate(
        test_resume.CONFIG.model_dump(mode="json")
        | {
            "validation": {
                "unit": {
                    "command": "grep -q 'x = 1[0-9][0-9]' src/app.py",
                    "required_for": ["standard"],
                }
            }
        }
    )
    # Pretend the checkpoint predates saved repair descriptions: nothing is written for it.
    monkeypatch.setattr(runner_module, "save_repair_scope", lambda store, scope: CONTRACT)

    class FailFirst(Seq):
        def run(self, request):
            result = super().run(request)
            if request.role == "implementation_worker" and self.worker_calls == 1:
                (request.cwd / "src" / "app.py").write_text("x = 2\n")
            return result

    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, FailFirst(timeout_on_call=2), config=config)
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = _store(git, first)
    assert store.latest_checkpoint()["next_stage"] == "validation_repair"
    assert not list(store.root.glob("execution/repair-validation-v*.json"))

    adapter = Seq(verdicts=["PASS"])
    resumed = resume_run(git, config, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert json.loads(_labels(adapter.requests[0], "repair_scope")[0])["gate"] == "validation"


def test_resumed_remediation_cannot_edit_beyond_the_named_files(tmp_path):
    finding = {
        "outcome": "CHANGES_REQUIRED",
        "findings": [
            {
                "id": "F001",
                "severity": "blocking",
                "summary": "fix app",
                "evidence_required": "fix",
                "file": "src/app.py",
            }
        ],
    }

    def stray(request, call):
        (request.cwd / "src" / "stray.py").write_text("z = 1\n")

    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(review=finding, timeout_on_call=2))
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    assert _store(git, first).latest_checkpoint()["next_stage"] == "remediation"

    with pytest.raises(ScopeViolation, match="stray.py"):
        resume_run(
            git,
            test_resume.CONFIG,
            test_resume.registry(Seq(review=finding, extra=stray)),
            first.run_id,
        )
