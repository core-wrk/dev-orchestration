import pytest
from pydantic import ValidationError

from dev_orchestration.domain.enums import Outcome
from dev_orchestration.domain.findings import (
    AcceptanceVerdict,
    Classification,
    Finding,
    OutcomeExplanation,
    ReviewResult,
    Severity,
    Verification,
    next_finding_id,
    verification_allows_completion,
)


def finding(identifier="F001", severity=Severity.BLOCKING):
    return Finding(id=identifier, severity=severity, summary="s", evidence_required="e")


def test_finding_ids_are_sequential_and_zero_padded():
    assert next_finding_id([]) == "F001"
    assert next_finding_id(["F001", "F009"]) == "F010"
    assert next_finding_id(["F001", "banana", "F0X2"]) == "F002"


def test_finding_is_immutable():
    with pytest.raises(ValidationError):
        finding().id = "F002"


def test_review_result_with_blocking_findings_cannot_be_pass():
    with pytest.raises(ValidationError):
        ReviewResult(outcome=Outcome.PASS, findings=[finding()])


def test_review_result_passes_with_only_minor_findings():
    assert (
        ReviewResult(
            outcome=Outcome.PASS, findings=[finding(severity=Severity.MINOR)]
        ).blocking_ids()
        == []
    )


def test_blocking_ids_returns_only_blocking():
    result = ReviewResult(
        outcome=Outcome.CHANGES_REQUIRED,
        findings=[finding("F001"), finding("F002", Severity.MINOR), finding("F003")],
    )
    assert result.blocking_ids() == ["F001", "F003"]


def test_acceptance_verdict_rejects_an_invented_verdict():
    with pytest.raises(ValidationError):
        AcceptanceVerdict(criterion="c", verdict="PROBABLY", evidence="e")


def test_verification_requires_a_verdict_for_every_criterion():
    with pytest.raises(ValidationError):
        Verification(outcome=Outcome.PASS, criteria=["a", "b"], verdicts=[])


def test_verification_rejects_extra_or_duplicate_criteria():
    with pytest.raises(ValidationError):
        Verification(
            outcome=Outcome.PASS,
            criteria=["a"],
            verdicts=[
                AcceptanceVerdict(criterion="a", verdict="PASS", evidence="e"),
                AcceptanceVerdict(criterion="a", verdict="PASS", evidence="e"),
            ],
        )


def test_verification_accepts_matching_verdicts():
    result = Verification(
        outcome=Outcome.PASS,
        criteria=["a"],
        verdicts=[AcceptanceVerdict(criterion="a", verdict="PASS", evidence="e")],
    )
    assert result.unresolved_finding_ids == []


def test_verification_allows_only_evidenced_minor_finding_explanations():
    minor = Finding(id="F001", severity=Severity.MINOR, summary="small note")
    result = Verification(
        outcome=Outcome.CHANGES_REQUIRED,
        criteria=["a"],
        verdicts=[AcceptanceVerdict(criterion="a", verdict="PASS", evidence="observed")],
        unresolved_finding_ids=["F001"],
        outcome_explanation=[OutcomeExplanation(finding_id="F001", note="minor note")],
    )
    assert verification_allows_completion(result, ["a"], [minor])


@pytest.mark.parametrize(
    ("outcome", "verdict", "evidence", "criteria", "unresolved", "explained", "severity"),
    [
        (Outcome.CHANGES_REQUIRED, "FAIL", "observed", ["a"], ["F001"], ["F001"], Severity.MINOR),
        (
            Outcome.CHANGES_REQUIRED,
            "UNKNOWN",
            "observed",
            ["a"],
            ["F001"],
            ["F001"],
            Severity.MINOR,
        ),
        (Outcome.CHANGES_REQUIRED, "PASS", " ", ["a"], ["F001"], ["F001"], Severity.MINOR),
        (
            Outcome.CHANGES_REQUIRED,
            "PASS",
            "observed",
            ["other"],
            ["F001"],
            ["F001"],
            Severity.MINOR,
        ),
        (
            Outcome.CHANGES_REQUIRED,
            "PASS",
            "observed",
            ["a"],
            ["missing"],
            ["missing"],
            Severity.MINOR,
        ),
        (
            Outcome.CHANGES_REQUIRED,
            "PASS",
            "observed",
            ["a"],
            ["F001"],
            ["F001"],
            Severity.IMPORTANT,
        ),
        (Outcome.BLOCKED, "PASS", "observed", ["a"], ["F001"], ["F001"], Severity.MINOR),
    ],
)
def test_verification_rejects_unsafe_minor_completion(
    outcome, verdict, evidence, criteria, unresolved, explained, severity
):
    result = Verification(
        outcome=outcome,
        criteria=["a"],
        verdicts=[AcceptanceVerdict(criterion="a", verdict=verdict, evidence=evidence)],
        unresolved_finding_ids=unresolved,
        outcome_explanation=[
            OutcomeExplanation(finding_id=identifier, note="note") for identifier in explained
        ],
    )
    known = [Finding(id="F001", severity=severity, summary="finding")]
    assert not verification_allows_completion(result, criteria, known)


def test_classification_rejects_a_tier_without_an_implemented_path():
    with pytest.raises(ValidationError):
        Classification(tier="high_risk", rationale="r", profiles=[])


def test_finding_below_blocking_may_omit_required_evidence():
    minor = Finding(id="F001", severity=Severity.MINOR, summary="s")
    important = Finding(id="F002", severity=Severity.IMPORTANT, summary="s")
    assert minor.evidence_required is None and important.evidence_required is None


def test_blocking_finding_must_state_required_evidence():
    with pytest.raises(ValidationError):
        Finding(id="F001", severity=Severity.BLOCKING, summary="s")
    with pytest.raises(ValidationError):
        Finding(id="F001", severity=Severity.BLOCKING, summary="s", evidence_required="   ")
