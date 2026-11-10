"""Phase 4: review findings handled by rule, at the earliest step that can fix them."""

import json
from dataclasses import replace
from datetime import UTC, datetime

from typer.testing import CliRunner

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.cli import app
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.findings import (
    Finding,
    Severity,
    Verification,
    verification_allows_completion,
)
from tests.test_cli_run import _failed_verification_source, _latest_manifest, _restart_cli
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_phase2 import Varying, _config, _labels, _manifest
from tests.workflow.test_runner import CRITERIA, registry, repo, script

IMPORTANT = {
    "id": "F001",
    "severity": "important",
    "summary": "older runs fail the checkpoint check",
    "evidence_required": None,
    "file": "src/app.py",
    "line": 1,
}
MINOR = IMPORTANT | {"severity": "minor", "summary": "message is vague", "file": "src/other.py"}
CLEAN = {"outcome": "PASS", "findings": []}


def _review(*findings, outcome="PASS"):
    return {"outcome": outcome, "findings": list(findings)}


def _supplied(request):
    """The findings the verifier was given, as the orchestrator numbered them."""
    return json.loads(_labels(request, "unresolved_findings")[0])


def _answer(request, *, verdict="PASS", cites=None, missing=None, unresolved=(), omit=()):
    """An answer about the supplied findings: those whose severity is in ``unresolved`` stay
    open, findings whose severity is in ``omit`` get no disposition, the rest are resolved.
    ``cites`` names severities whose findings the verdict rests on."""
    findings = _supplied(request)
    return {
        "outcome": "PASS" if verdict == "PASS" else "CHANGES_REQUIRED",
        "verdicts": [
            {
                "criterion_id": "AC1",
                "verdict": verdict,
                "evidence": "seen" if verdict == "PASS" else "not met",
                "missing_evidence": missing,
                "finding_ids": [f["id"] for f in findings if f["severity"] in (cites or ())]
                or None,
            }
        ],
        "finding_dispositions": [
            {
                "finding_id": f["id"],
                "status": "unresolved" if f["severity"] in unresolved else "resolved",
                "evidence": "still open" if f["severity"] in unresolved else "fixed in the diff",
            }
            for f in findings
            if f["severity"] not in omit
        ],
    }


class Run(Varying):
    """Varying worker; the verifier's answers are built from what it is supplied."""

    def __init__(self, reviews, answers, *, extra=None):
        super().__init__(script() | {"implementation_reviewer": list(reviews)}, extra=extra)
        self.answers = list(answers)

    def run(self, request):
        if request.role != "verifier":
            return super().run(request)
        self.seen.append(request.role)
        self.requests.append(request)
        make = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, make(request), now, now)


def _go(tmp_path, adapter):
    return execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )


def _workers(adapter):
    return [r for r in adapter.requests if r.role == "implementation_worker"]


def _scope(request):
    return json.loads(_labels(request, "repair_scope")[0])


# --- 1. important findings are repaired at review ---------------------------------------


def test_an_important_finding_naming_a_file_is_repaired_at_review_before_verification(tmp_path):
    adapter = Run([_review(IMPORTANT), CLEAN], [_answer])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    scope = _scope(_workers(adapter)[1])
    assert (scope["gate"], scope["files"], scope["restricted"]) == (
        "implementation_review",
        ["src/app.py"],
        True,
    )
    roles = [role for role in adapter.seen if role != "implementation_worker"]
    assert roles[-3:] == ["implementation_reviewer", "implementation_reviewer", "verifier"]


def test_an_important_finding_naming_no_file_goes_straight_to_verification(tmp_path):
    adapter = Run([_review(IMPORTANT | {"file": None, "line": None})], [_answer])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 1


def test_important_findings_left_after_the_first_review_round_go_on_to_verification(tmp_path):
    # Only the first review round repairs important findings; later rounds are for blocking ones,
    # so a regression found late still has a round. What is left goes to final verification.
    adapter = Run([_review(IMPORTANT)], [_answer])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    assert adapter.seen.count("verifier") == 1


def test_a_blocking_finding_in_the_second_review_round_is_still_repaired(tmp_path):
    blocking = IMPORTANT | {"id": "F002", "severity": "blocking", "evidence_required": "a test"}
    changes = _review(blocking, outcome="CHANGES_REQUIRED")
    adapter = Run([_review(IMPORTANT), changes, CLEAN], [_answer])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 3
    assert _scope(_workers(adapter)[2])["gate"] == "implementation_review"


def test_important_findings_are_repaired_only_in_the_first_review_round():
    from dev_orchestration.domain.findings import ReviewResult
    from dev_orchestration.workflow.contract import review_repair_findings

    blocking = {"id": "F002", "severity": "blocking", "summary": "s", "evidence_required": "e"}
    review = ReviewResult.model_validate(
        {"outcome": "CHANGES_REQUIRED", "findings": [IMPORTANT, blocking]}
    )

    assert [f.id for f in review_repair_findings(review, 1)] == ["F002", "F001"]
    assert [f.id for f in review_repair_findings(review, 2)] == ["F002"]
    assert [f.id for f in review_repair_findings(review, 3)] == ["F002"]


# --- 2. every finding gets a disposition ------------------------------------------------

NO_FILE = IMPORTANT | {"file": None, "line": None}


def test_an_answer_omitting_an_important_findings_disposition_is_retried_once(tmp_path):
    adapter = Run(
        [_review(NO_FILE)],
        [lambda r: _answer(r, omit=("important",)), _answer],
    )
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen.count("verifier") == 2


def test_an_answer_that_keeps_omitting_a_disposition_escalates(tmp_path):
    adapter = Run([_review(NO_FILE)], [lambda r: _answer(r, omit=("important",))])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.ESCALATED
    assert "finding" in (_manifest(outcome).terminal_reason or "")


def test_a_pass_with_an_important_finding_unresolved_gets_a_findings_repair(tmp_path):
    # The first review round does not clear it; final verification still finds it open.
    adapter = Run(
        [_review(IMPORTANT), _review(IMPORTANT), CLEAN],
        [lambda r: _answer(r, unresolved=("important",)), _answer],
    )
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    findings_repair = _workers(adapter)[2]
    scope = _scope(findings_repair)
    assert (scope["gate"], scope["mode"], scope["files"]) == (
        "final_verification",
        "findings",
        ["src/app.py"],
    )


def test_a_pass_with_an_unresolved_important_finding_no_longer_completes():
    finding = Finding(id="F001", severity=Severity.IMPORTANT, summary="s")
    answered = Verification.model_validate(
        {
            "outcome": "PASS",
            "criteria": ["c"],
            "verdicts": [{"criterion": "c", "verdict": "PASS", "evidence": "e"}],
            "unresolved_finding_ids": ["F001"],
            "outcome_explanation": [{"finding_id": "F001", "note": "open"}],
            "finding_dispositions": [
                {"finding_id": "F001", "status": "unresolved", "evidence": "open"}
            ],
        }
    )
    assert not verification_allows_completion(answered, ["c"], [finding])


# --- 3. minor findings never fail a criterion -------------------------------------------


def test_a_fail_resting_only_on_minor_findings_completes_with_a_note(tmp_path):
    adapter = Run(
        [_review(MINOR)],
        [lambda r: _answer(r, verdict="FAIL", cites=("minor",), unresolved=("minor",))],
    )
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 1
    saved = json.loads((outcome.store_root / "verification/final-verification.json").read_text())
    verdict = saved["verdicts"][0]
    assert (verdict["verdict"], verdict["original_verdict"]) == ("PASS", "FAIL")
    assert saved["unresolved_finding_ids"] == [_supplied(adapter.requests[-1])[0]["id"]]


def test_a_fail_citing_no_finding_is_still_a_product_repair(tmp_path):
    adapter = Run([CLEAN], [lambda r: _answer(r, verdict="FAIL"), _answer])
    outcome = _go(tmp_path, adapter)

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert _scope(_workers(adapter)[1])["mode"] == "product"


# --- 4. restarts may ask once more ------------------------------------------------------


def _restart(tmp_path, monkeypatch, *, review, answers):
    root, source_id, adapter = _failed_verification_source(tmp_path, monkeypatch)
    _restart_cli(monkeypatch, tmp_path, adapter)
    monkeypatch.chdir(root)
    original = adapter.run
    queue = list(answers)

    def scripted(request):
        result = original(request)
        if request.role == "implementation_reviewer":
            return replace(result, output=review)
        if request.role == "verifier":
            make = queue.pop(0) if len(queue) > 1 else queue[0]
            return replace(result, output=make(request))
        return result

    adapter.run = scripted
    adapter.seen.clear()
    result = CliRunner().invoke(
        app, ["restart", source_id, "--from", "validation", "--reason", "recheck"]
    )
    return result, adapter, _latest_manifest(root)


def test_a_check_only_restart_whose_unknown_wants_a_file_asks_once_more(tmp_path, monkeypatch):
    _, adapter, manifest = _restart(
        tmp_path,
        monkeypatch,
        review=CLEAN,
        answers=[lambda r: _answer(r, verdict="UNKNOWN", missing=["file"]), _answer],
    )

    assert manifest.restart_mode == "check_only"
    assert manifest.status is RunState.COMPLETE_LOCAL, manifest.terminal_reason
    assert adapter.seen.count("verifier") == 2
    assert manifest.verification_ask_rounds == [0]


def test_a_restart_with_minor_only_notes_completes(tmp_path, monkeypatch):
    _, _, manifest = _restart(
        tmp_path,
        monkeypatch,
        review=_review(MINOR),
        answers=[lambda r: _answer(r, unresolved=("minor",))],
    )

    assert manifest.status is RunState.COMPLETE_LOCAL, manifest.terminal_reason


# --- 5 and 6. every answer kept; the worker's report labelled ---------------------------


def test_every_verifier_answer_is_kept(tmp_path):
    adapter = Run([CLEAN], [lambda r: _answer(r, verdict="FAIL"), _answer])
    outcome = _go(tmp_path, adapter)

    kept = sorted(p.name for p in (outcome.store_root / "verification").glob("final-*-v*.json"))
    assert kept == ["final-verification-v1.json", "final-verification-v2.json"]
    first = json.loads((outcome.store_root / "verification/final-verification-v1.json").read_text())
    assert first["verdicts"][0]["verdict"] == "FAIL"


def test_the_workers_report_is_labelled_as_its_own_account(tmp_path):
    adapter = Run([CLEAN], [_answer])
    _go(tmp_path, adapter)

    verifier = next(r for r in adapter.requests if r.role == "verifier")
    facts = _labels(verifier, "run_facts")[0]
    assert "sandbox without network" in facts and "not validation evidence" in facts


# --- older answers ----------------------------------------------------------------------


def test_an_answer_saved_before_this_phase_loads_and_routes_as_before():
    minor = Finding(id="F001", severity=Severity.MINOR, summary="s")
    important = Finding(id="F002", severity=Severity.IMPORTANT, summary="s")
    legacy = {
        "outcome": "CHANGES_REQUIRED",
        "criteria": ["c"],
        "verdicts": [{"criterion": "c", "verdict": "PASS", "evidence": "e"}],
        "unresolved_finding_ids": ["F001"],
        "outcome_explanation": [{"finding_id": "F001", "note": "minor"}],
    }
    saved = Verification.model_validate(legacy)
    assert saved.finding_dispositions is None
    assert verification_allows_completion(saved, ["c"], [minor])
    # As before this phase: an important finding anywhere in the review blocks this path.
    assert not verification_allows_completion(saved, ["c"], [minor, important])


def test_a_blocking_finding_naming_no_file_keeps_the_review_repair_unrestricted():
    from dev_orchestration.config.models import Scope
    from dev_orchestration.domain.findings import ReviewResult
    from dev_orchestration.scope import ScopeFence
    from dev_orchestration.workflow.contract import review_repair_scope

    blocking = {"id": "F001", "severity": "blocking", "summary": "s", "evidence_required": "e"}
    review = ReviewResult.model_validate(
        {"outcome": "CHANGES_REQUIRED", "findings": [blocking, IMPORTANT | {"id": "F002"}]}
    )
    scope = review_repair_scope(review, 1, ScopeFence.from_config(Scope(include=["src/"])))

    assert (scope["check"], scope["restricted"]) == ("F001, F002", False)
