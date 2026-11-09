"""Structured results returned by classification, review, and verification."""

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from dev_orchestration.domain.enums import Outcome, Tier

_FINDING_ID = re.compile(r"^F(\d{3,})$")
IMPLEMENTED_TIERS = frozenset({Tier.TRIVIAL, Tier.STANDARD, Tier.SUBSTANTIAL})


class Severity(StrEnum):
    BLOCKING = "blocking"
    IMPORTANT = "important"
    MINOR = "minor"


def next_finding_id(existing: list[str]) -> str:
    numbers = [int(match.group(1)) for item in existing if (match := _FINDING_ID.match(item))]
    return f"F{max(numbers, default=0) + 1:03d}"


class Finding(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    id: str
    severity: Severity
    summary: str
    # Optional below BLOCKING. A mandatory field on every finding compels reviewers to
    # manufacture an evidence demand for issues that do not warrant one, which is how
    # evidence theater gets encoded in the schema rather than merely in the prompt.
    evidence_required: str | None = None
    file: str | None = None
    line: int | None = None

    @model_validator(mode="after")
    def blocking_findings_state_required_evidence(self) -> Self:
        if self.severity is Severity.BLOCKING and not (
            self.evidence_required and self.evidence_required.strip()
        ):
            raise ValueError(
                f"blocking finding {self.id} must state the evidence required to resolve it"
            )
        return self


class ReviewResult(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    outcome: Outcome
    findings: list[Finding] = Field(default_factory=list)

    def blocking_ids(self) -> list[str]:
        return [finding.id for finding in self.findings if finding.severity is Severity.BLOCKING]

    @model_validator(mode="after")
    def pass_cannot_carry_blocking_findings(self) -> Self:
        if self.outcome is Outcome.PASS and self.blocking_ids():
            raise ValueError(f"outcome PASS contradicts blocking findings {self.blocking_ids()}")
        return self


class Classification(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    tier: Tier
    rationale: str
    profiles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def tier_is_implemented(self) -> Self:
        if self.tier not in IMPLEMENTED_TIERS:
            raise ValueError(
                f"tier {self.tier!r} has no implemented path; "
                f"implemented tiers are {sorted(IMPLEMENTED_TIERS)}"
            )
        return self


class DependencyFinding(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    relationship: Literal["prerequisite", "shared_contract", "conflict", "advisory"]
    source: str
    excerpt: str
    current_behavior: str
    future_work: str
    approval_status: Literal["approved", "draft", "ambiguous", "not_applicable"]
    effect_on_task: str


class DependencyReport(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    findings: list[DependencyFinding] = Field(default_factory=list)
    searched_sources: list[str] = Field(default_factory=list)
    inaccessible_sources: list[str] = Field(default_factory=list)
    incomplete_searches: list[str] = Field(default_factory=list)
    budget_exhausted: bool = False


MissingKind = Literal["test", "command", "file", "run_fact", "other"]


def _as_kind_list(value: object) -> object:
    return [value] if isinstance(value, str) else value


class AcceptanceVerdict(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    criterion: str
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    evidence: str
    # Every kind of evidence that would settle an UNKNOWN verdict. Answers saved before this
    # field existed have none, and the orchestrator treats that like "other": not something it
    # or a worker repair can supply. Answers saved with a single kind load as a one-item list.
    missing_evidence: list[MissingKind] | None = None

    _one_kind_is_a_list = field_validator("missing_evidence", mode="before")(_as_kind_list)


class OutcomeExplanation(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    finding_id: str
    note: str


class Verification(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    outcome: Outcome
    criteria: list[str] = Field(default_factory=list)
    verdicts: list[AcceptanceVerdict] = Field(default_factory=list)
    unresolved_finding_ids: list[str] = Field(default_factory=list)
    outcome_explanation: list[OutcomeExplanation] = Field(default_factory=list)

    @model_validator(mode="after")
    def every_criterion_has_a_verdict(self) -> Self:
        expected = set(self.criteria)
        judged = [verdict.criterion for verdict in self.verdicts]
        missing = [criterion for criterion in self.criteria if criterion not in set(judged)]
        extras = [criterion for criterion in judged if criterion not in expected]
        duplicates = [criterion for criterion in set(judged) if judged.count(criterion) > 1]
        if missing or extras or duplicates:
            raise ValueError(
                "verification verdicts must match criteria exactly: "
                f"missing={missing}, extras={extras}, duplicates={duplicates}"
            )
        return self


def criterion_ids(criteria: list[str]) -> list[str]:
    return [f"AC{index}" for index in range(1, len(criteria) + 1)]


class CriterionVerdict(BaseModel):
    """One verdict as the verifier returns it: the criterion is named by its supplied ID."""

    model_config = {"frozen": True, "extra": "forbid"}

    criterion_id: str
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    evidence: str
    missing_evidence: list[MissingKind] | None = None

    _one_kind_is_a_list = field_validator("missing_evidence", mode="before")(_as_kind_list)


class VerifierAnswer(BaseModel):
    """What the verifier returns. The orchestrator, not the verifier, supplies criteria text.

    Validated with ``context={"criterion_ids": [...]}`` so an answer that misses, repeats, or
    invents an ID fails validation and gets the one corrective retry.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    outcome: Outcome
    verdicts: list[CriterionVerdict] = Field(default_factory=list)
    unresolved_finding_ids: list[str] = Field(default_factory=list)
    outcome_explanation: list[OutcomeExplanation] = Field(default_factory=list)

    @model_validator(mode="after")
    def every_supplied_id_has_one_verdict(self, info: ValidationInfo) -> Self:
        expected = (info.context or {}).get("criterion_ids")
        if expected is None:
            return self
        judged = [verdict.criterion_id for verdict in self.verdicts]
        missing = [cid for cid in expected if cid not in judged]
        unknown = [cid for cid in judged if cid not in expected]
        duplicates = sorted({cid for cid in judged if judged.count(cid) > 1})
        if missing or unknown or duplicates:
            raise ValueError(
                f"verdicts must give exactly one verdict for each supplied criterion ID "
                f"{expected}: missing={missing}, unknown={unknown}, duplicates={duplicates}"
            )
        return self

    def to_verification(self, criteria: list[str]) -> Verification:
        by_id = {verdict.criterion_id: verdict for verdict in self.verdicts}
        return Verification(
            outcome=self.outcome,
            criteria=list(criteria),
            verdicts=[
                AcceptanceVerdict(
                    criterion=criterion,
                    verdict=by_id[cid].verdict,
                    evidence=by_id[cid].evidence,
                    missing_evidence=by_id[cid].missing_evidence,
                )
                for cid, criterion in zip(criterion_ids(criteria), criteria, strict=True)
            ],
            unresolved_finding_ids=list(self.unresolved_finding_ids),
            outcome_explanation=list(self.outcome_explanation),
        )


def verification_allows_completion(
    verification: Verification,
    criteria: list[str],
    findings: list[Finding],
) -> bool:
    """Accept a PASS, or all-PASS criteria with notes about known minor findings."""
    verdict_criteria = [verdict.criterion for verdict in verification.verdicts]
    if (
        verification.criteria != criteria
        or verdict_criteria != criteria
        or any(
            verdict.verdict != "PASS" or not verdict.evidence.strip()
            for verdict in verification.verdicts
        )
    ):
        return False

    finding_by_id = {finding.id: finding for finding in findings}
    unresolved = verification.unresolved_finding_ids
    explained = [item.finding_id for item in verification.outcome_explanation]
    if len(set(unresolved)) != len(unresolved) or len(set(explained)) != len(explained):
        return False
    if any(not item.note.strip() for item in verification.outcome_explanation):
        return False
    if any(identifier not in finding_by_id for identifier in [*unresolved, *explained]):
        return False
    if any(finding.severity is Severity.BLOCKING for finding in findings):
        return False

    if verification.outcome is Outcome.PASS and not verification.outcome_explanation:
        return not any(
            finding_by_id[identifier].severity is Severity.BLOCKING for identifier in unresolved
        )
    # Notes on known minor findings are accepted whether the verifier labelled its answer PASS or
    # CHANGES_REQUIRED; the label does not change what was found.
    if verification.outcome not in {Outcome.PASS, Outcome.CHANGES_REQUIRED} or not explained:
        return False
    return (
        set(explained) == set(unresolved)
        and all(finding.severity is Severity.MINOR for finding in findings)
        and all(finding_by_id[identifier].severity is Severity.MINOR for identifier in explained)
    )
