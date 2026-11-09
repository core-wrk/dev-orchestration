"""Phase 3a: "not demonstrated" (UNKNOWN) verdicts that need a test go back to the worker."""

import json
from datetime import UTC, datetime

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.findings import Verification, VerifierAnswer
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.schemas import SCHEMA_MODELS, schema_for
from dev_orchestration.workflow.recovery import resume_run
from tests.workflow import test_resume
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_phase2 import (
    Seq,
    Varying,
    _first_run,
    _labels,
    _manifest,
    _store,
    _verdict,
)
from tests.workflow.test_runner import CONFIG, CRITERIA, registry, repo, script


def _unknown(missing, outcome="BLOCKED", criteria=CRITERIA):
    verdict = {"criterion_id": "AC1", "verdict": "UNKNOWN", "evidence": "not shown"}
    if missing is not None:
        verdict["missing_evidence"] = missing
    return dict(_verdict("UNKNOWN", criteria), outcome=outcome, verdicts=[verdict])


def _add_test(request, call):
    if call >= 2:
        (request.cwd / "src" / "test_app.py").write_text(f"def test_flag():\n    assert {call}\n")


def _execute(tmp_path, verifier, extra=_add_test):
    git = repo(tmp_path)
    adapter = Varying(script() | {"verifier": verifier}, extra=extra)
    outcome = execute_run(git, CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA)
    return outcome, adapter


def _repairs(adapter):
    return [r for r in adapter.requests if r.role == "implementation_worker"][1:]


# --- the verifier's answer ----------------------------------------------------------------


def test_verdict_names_what_is_missing_and_older_answers_still_load():
    saved = {
        "outcome": "CHANGES_REQUIRED",
        "criteria": CRITERIA,
        "verdicts": [{"criterion": CRITERIA[0], "verdict": "UNKNOWN", "evidence": "x"}],
    }
    assert Verification.model_validate(saved).verdicts[0].missing_evidence is None

    new = VerifierAnswer.model_validate(_unknown("test")).to_verification(CRITERIA)
    assert new.verdicts[0].missing_evidence == ["test"]

    schema = schema_for(SCHEMA_MODELS["verification"])["$defs"]["CriterionVerdict"]
    assert "missing_evidence" in schema["required"]


def test_verifier_role_asks_for_the_missing_kind_and_the_test_behind_a_pass():
    prompt = load_role_prompt("verifier")
    assert "missing_evidence" in prompt
    assert "name the test" in prompt


def test_worker_role_explains_evidence_repairs():
    assert "evidence repair" in load_role_prompt("implementation_worker").lower()


# --- new runs ----------------------------------------------------------------------------


def test_blocked_with_only_missing_test_verdicts_gets_an_evidence_repair(tmp_path):
    outcome, adapter = _execute(tmp_path, [_unknown("test"), _verdict("PASS")])

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    assert adapter.seen.count("verifier") == 2
    repair = _repairs(adapter)[0]
    scope = json.loads(_labels(repair, "repair_scope")[0])
    assert (scope["gate"], scope["mode"]) == ("final_verification", "evidence")
    assert "evidence repair" in _labels(repair, "blocking_findings")[0].lower()
    assert _manifest(outcome).repair_rounds["final_verification"] == 1


def test_changes_required_with_only_missing_test_verdicts_takes_the_same_path(tmp_path):
    first = [_unknown("test", outcome="CHANGES_REQUIRED"), _verdict("PASS")]
    outcome, adapter = _execute(tmp_path, first)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    scope = json.loads(_labels(_repairs(adapter)[0], "repair_scope")[0])
    assert scope["mode"] == "evidence"


def test_unknown_missing_a_command_run_escalates_without_a_worker_call(tmp_path):
    outcome, adapter = _execute(tmp_path, [_unknown("command", outcome="CHANGES_REQUIRED")])

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1
    assert _manifest(outcome).terminal_cause == "verification_escalated"


def test_unknown_without_a_missing_kind_escalates(tmp_path):
    outcome, adapter = _execute(tmp_path, [_unknown(None, outcome="CHANGES_REQUIRED")])

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1
    assert _manifest(outcome).terminal_cause == "verification_escalated"


def test_blocked_with_a_failed_criterion_is_repaired_like_any_failure(tmp_path):
    blocked = dict(_verdict("FAIL"), outcome="BLOCKED")
    outcome, adapter = _execute(tmp_path, [blocked, _verdict("PASS")], extra=None)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    scope = json.loads(_labels(_repairs(adapter)[0], "repair_scope")[0])
    assert scope["mode"] == "product"


def test_evidence_repair_that_changes_no_test_file_is_no_progress(tmp_path):
    outcome, adapter = _execute(tmp_path, [_unknown("test")], extra=None)

    assert adapter.worker_calls == 2
    assert _manifest(outcome).terminal_cause == "remediation_no_progress"
    assert "test file" in (_manifest(outcome).terminal_reason or "")


def test_verification_gets_three_repair_rounds_then_stops(tmp_path):
    outcome, adapter = _execute(tmp_path, [_verdict("FAIL")], extra=None)

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 4
    manifest = _manifest(outcome)
    assert manifest.stop.repairs["final_verification"].model_dump() == {
        "used": 3,
        "allowed": 3,
        "exhausted": True,
    }
    assert manifest.terminal_cause == "verification_exhausted"


# --- resume ------------------------------------------------------------------------------


class SeqAnswers(Seq):
    """Resume-side adapter whose verifier returns whole scripted answers."""

    def __init__(self, answers, **kwargs):
        super().__init__(verdicts=["PASS"], **kwargs)
        self.answers = list(answers)

    def run(self, request):
        if request.role == "verifier":
            self.seen.append(request.role)
            self.requests.append(request)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            now = datetime.now(UTC)
            return AgentResult(self.name, None, 0, answer, now, now)
        return super().run(request)


def test_interrupted_evidence_repair_resumes_as_an_evidence_repair(tmp_path):
    criteria = test_resume.CRITERIA
    git = test_resume.repo(tmp_path)
    first = _first_run(
        git, tmp_path, SeqAnswers([_unknown("test", criteria=criteria)], timeout_on_call=2)
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    assert _store(git, first).latest_checkpoint()["next_stage"] == "verification_repair"

    # The resumed worker's first call is the interrupted evidence repair.
    adapter = SeqAnswers(
        [_verdict("PASS", criteria)], extra=lambda request, call: _add_test(request, call + 1)
    )
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    scope = json.loads(_labels(adapter.requests[0], "repair_scope")[0])
    assert scope["mode"] == "evidence"


def test_resumed_evidence_repair_without_a_test_change_is_no_progress(tmp_path):
    criteria = test_resume.CRITERIA
    git = test_resume.repo(tmp_path)
    first = _first_run(
        git, tmp_path, SeqAnswers([_unknown("test", criteria=criteria)], timeout_on_call=2)
    )

    adapter = SeqAnswers([_verdict("PASS", criteria)])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is not RunState.COMPLETE_LOCAL
    assert _store(git, first).read_manifest().terminal_cause == "remediation_no_progress"


def test_resumed_rejection_routes_by_verdicts(tmp_path):
    criteria = test_resume.CRITERIA
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1))

    adapter = SeqAnswers(
        [_unknown("test", criteria=criteria), _verdict("PASS", criteria)], extra=_add_test
    )
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2


def test_resumed_run_with_an_older_unknown_answer_escalates(tmp_path):
    criteria = test_resume.CRITERIA
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1))

    adapter = SeqAnswers([_unknown(None, outcome="CHANGES_REQUIRED", criteria=criteria)])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1
    assert _store(git, first).read_manifest().terminal_cause == "verification_escalated"
