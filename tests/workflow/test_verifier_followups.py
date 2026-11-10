"""Verifier answers name criteria by ID; a rejection resting only on review findings is repaired."""

import json
from datetime import UTC, datetime

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.domain.enums import RunState
from dev_orchestration.schemas import SCHEMA_MODELS, schema_for
from dev_orchestration.workflow.recovery import resume_run
from tests.workflow import test_resume
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_phase2 import (
    Seq,
    Varying,
    _config,
    _first_run,
    _labels,
    _manifest,
    _store,
)
from tests.workflow.test_runner import CONFIG, Scripted, registry, repo, script

CRITERIA = ["the `flag` exists", "existing callers still work"]
FINDING = {
    "id": "F001",
    "severity": "important",
    "summary": "older runs may fail the checkpoint check",
    "evidence_required": None,
    "file": "src/app.py",
    "line": 1,
}


def _answer(*verdicts, outcome="PASS", unresolved=(), resolved=()):
    return {
        "outcome": outcome,
        "verdicts": [
            {"criterion_id": cid, "verdict": verdict, "evidence": "seen", "missing_evidence": None}
            for cid, verdict in verdicts
        ],
        "finding_dispositions": [
            {"finding_id": fid, "status": "unresolved", "evidence": "still open"}
            for fid in unresolved
        ]
        + [{"finding_id": fid, "status": "resolved", "evidence": "fixed"} for fid in resolved],
    }


ALL_PASS = _answer(("AC1", "PASS"), ("AC2", "PASS"))
FINDING_ONLY = _answer(
    ("AC1", "PASS"), ("AC2", "PASS"), outcome="CHANGES_REQUIRED", unresolved=["F001"]
)
CLEAN = {"outcome": "PASS", "findings": []}


def _saved(outcome):
    return json.loads((outcome.store_root / "verification/final-verification.json").read_text())


# --- criteria by ID ----------------------------------------------------------------------


def test_verifier_sees_numbered_criteria_and_its_ids_map_back_to_the_exact_text(tmp_path):
    reordered = _answer(("AC2", "PASS"), ("AC1", "PASS"))
    adapter = Scripted(script(verifier=reordered))
    outcome = execute_run(
        repo(tmp_path), CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    verifier = next(r for r in adapter.requests if r.role == "verifier")
    assert _labels(verifier, "acceptance_criteria") == [
        "AC1: the `flag` exists\nAC2: existing callers still work"
    ]
    saved = _saved(outcome)
    assert saved["criteria"] == CRITERIA
    assert [verdict["criterion"] for verdict in saved["verdicts"]] == CRITERIA


def test_provider_schema_asks_for_criterion_ids_not_criterion_text():
    schema = schema_for(SCHEMA_MODELS["verification"])
    verdict = next(
        item for item in schema["$defs"].values() if "criterion_id" in item.get("properties", {})
    )
    assert "criterion" not in verdict["properties"]
    assert "criteria" not in schema["properties"]


def test_an_answer_with_a_missing_or_unknown_id_is_retried_once(tmp_path):
    bad = _answer(("AC1", "PASS"), ("AC9", "PASS"))
    adapter = Scripted(script() | {"verifier": [bad, ALL_PASS]})
    outcome = execute_run(
        repo(tmp_path), CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen.count("verifier") == 2
    retry = [r for r in adapter.requests if r.role == "verifier"][1]
    assert "AC9" in retry.prompt


def test_an_answer_that_never_covers_every_id_escalates(tmp_path):
    bad = _answer(("AC1", "PASS"))
    adapter = Scripted(script(verifier=bad))
    outcome = execute_run(
        repo(tmp_path), CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.ESCALATED
    assert "missing=['AC2']" in (_manifest(outcome).terminal_reason or "")


# --- rejections that rest only on review findings ----------------------------------------
# Since Phase 4 an important finding naming a file is first repaired at review (first round
# only), so these runs keep it open through that round to reach the verification findings repair:
# worker calls are the implementation, one review repair, then the findings repair.


def _finding_run(
    tmp_path, finding=FINDING, verifier=(FINDING_ONLY, ALL_PASS), extra=None, reviews=None
):
    review = {"outcome": "PASS", "findings": [finding]}
    adapter = Varying(
        script()
        | {"implementation_reviewer": reviews or [review, review, CLEAN]}
        | {"verifier": list(verifier)},
        extra=extra,
    )
    outcome = execute_run(
        repo(tmp_path), CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA
    )
    return outcome, adapter


def test_all_pass_rejection_over_an_unresolved_finding_gets_a_findings_repair(tmp_path):
    outcome, adapter = _finding_run(tmp_path)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 3
    repair = [r for r in adapter.requests if r.role == "implementation_worker"][2]
    scope = json.loads(_labels(repair, "repair_scope")[0])
    assert (scope["gate"], scope["mode"]) == ("final_verification", "findings")
    assert (scope["files"], scope["restricted"]) == (["src/app.py"], True)
    assert scope["check"] == "F001"
    findings_text = _labels(repair, "blocking_findings")[0]
    assert "F001" in findings_text and "older runs may fail" in findings_text
    assert _manifest(outcome).repair_rounds["final_verification"] == 1


def test_findings_repair_may_not_change_files_the_findings_do_not_name(tmp_path):
    def stray(request, call):
        if call == 3:
            (request.cwd / "src" / "other.py").write_text("y = 1\n")

    outcome, _ = _finding_run(tmp_path, extra=stray)

    assert outcome.final_state is not RunState.COMPLETE_LOCAL
    assert "src/other.py" in (_manifest(outcome).terminal_reason or "")


def test_finding_that_names_no_file_is_not_repaired(tmp_path):
    no_file = {"outcome": "PASS", "findings": [FINDING | {"file": None, "line": None}]}
    outcome, adapter = _finding_run(tmp_path, reviews=[no_file])

    assert outcome.final_state is RunState.FAILED
    assert adapter.worker_calls == 1
    assert adapter.seen.count("verifier") == 1


def test_resumed_finding_only_rejection_gets_a_findings_repair(tmp_path):
    finding_only = _answer(("AC1", "PASS"), outcome="CHANGES_REQUIRED", unresolved=["F001"])
    review = {"outcome": "PASS", "findings": [FINDING]}
    git = test_resume.repo(tmp_path)
    first = _first_run(git, tmp_path, Seq(timeout_on_call=1, review=review))
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    class Answers(Seq):
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

    adapter = Answers([finding_only, _answer(("AC1", "PASS"), resolved=["F001"])], review=review)
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    # Implementation, one review repair that leaves F001 open, then the findings repair.
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 3
    workers = [r for r in adapter.requests if r.role == "implementation_worker"]
    assert json.loads(_labels(workers[2], "repair_scope")[0])["mode"] == "findings"
    assert _store(git, first).read_manifest().verification_repairs_reserved == 1


# --- minor findings never block or force a repair ---------------------------------------

MINOR = FINDING | {"id": "F002", "severity": "minor", "summary": "status message is vague"}
# Review findings are numbered by the orchestrator, so a lone minor finding is F001.
PASS_WITH_NOTES = _answer(("AC1", "PASS"), ("AC2", "PASS"), unresolved=["F001"])


def test_pass_with_notes_on_minor_findings_completes_without_a_repair(tmp_path):
    minor = {"outcome": "PASS", "findings": [MINOR]}
    outcome, adapter = _finding_run(tmp_path, verifier=(PASS_WITH_NOTES,), reviews=[minor])

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 1


def test_pass_with_notes_on_an_important_finding_is_repaired(tmp_path):
    important = _answer(("AC1", "PASS"), ("AC2", "PASS"), unresolved=["F001"])
    outcome, adapter = _finding_run(tmp_path, verifier=(important, ALL_PASS))

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 3


def test_findings_repair_covers_only_the_findings_that_are_not_minor(tmp_path):
    # Both the review repair and, later, the findings repair take only the important finding.
    review = {"outcome": "PASS", "findings": [FINDING, MINOR | {"file": "src/other.py"}]}
    both = _answer(
        ("AC1", "PASS"), ("AC2", "PASS"), outcome="CHANGES_REQUIRED", unresolved=["F001", "F002"]
    )
    adapter = Varying(
        script()
        | {"implementation_reviewer": [review, review, CLEAN]}
        | {"verifier": [both, ALL_PASS]}
    )
    outcome = execute_run(
        repo(tmp_path), CONFIG, registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    repair = [r for r in adapter.requests if r.role == "implementation_worker"][1]
    scope = json.loads(_labels(repair, "repair_scope")[0])
    assert (scope["check"], scope["files"]) == ("F001", ["src/app.py"])


# --- each worker change gets its own validation repairs ----------------------------------


def test_a_verification_repair_that_breaks_validation_gets_its_own_validation_repairs(tmp_path):
    # Worker calls write x = 2, 3, 4, ...; validation passes only at x = 4 and x = 6.
    # Calls 1-3: implementation plus both of its validation repairs. Call 4: the verification
    # repair, which breaks validation. Call 5: a validation repair the old per-run budget denied.
    adapter = Varying(
        script()
        | {
            "verifier": [
                _answer(("AC1", "FAIL"), ("AC2", "PASS"), outcome="CHANGES_REQUIRED"),
                ALL_PASS,
            ]
        }
    )
    outcome = execute_run(
        repo(tmp_path),
        _config("grep -qxE 'x = (4|6)' src/app.py"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 5
    manifest = _manifest(outcome)
    assert manifest.validation_repairs_reserved == 3
    assert manifest.verification_repairs_reserved == 1


def test_validation_repairs_stay_bounded_after_a_verification_repair(tmp_path):
    adapter = Varying(
        script()
        | {
            "verifier": [
                _answer(("AC1", "FAIL"), ("AC2", "PASS"), outcome="CHANGES_REQUIRED"),
                ALL_PASS,
            ]
        }
    )
    outcome = execute_run(
        repo(tmp_path),
        _config("grep -qxE 'x = 4' src/app.py"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        CRITERIA,
    )

    # The verification repair (call 4) breaks validation; its two repairs (calls 5, 6) never fix it.
    assert outcome.final_state is RunState.FAILED
    assert adapter.worker_calls == 6
    manifest = _manifest(outcome)
    assert manifest.terminal_cause == "validation_failed"
