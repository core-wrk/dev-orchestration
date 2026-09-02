import pytest
from pydantic import ValidationError

from dev_orchestration.domain.enums import Outcome
from dev_orchestration.domain.findings import (
    AcceptanceVerdict,
    Classification,
    Finding,
    ReviewResult,
    Severity,
    Verification,
    next_finding_id,
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


def test_classification_rejects_a_tier_without_an_implemented_path():
    with pytest.raises(ValidationError):
        Classification(tier="high_risk", rationale="r", profiles=[])
