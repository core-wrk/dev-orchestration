"""Structured results returned by classification, review, and verification."""

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from dev_orchestration.domain.enums import Outcome, Tier

_FINDING_ID = re.compile(r"^F(\d{3,})$")
M2_TIERS = frozenset({Tier.TRIVIAL, Tier.STANDARD})


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
    evidence_required: str
    file: str | None = None
    line: int | None = None


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
        if self.tier not in M2_TIERS:
            raise ValueError(
                f"tier {self.tier!r} has no implemented path in M2; "
                f"implemented tiers are {sorted(M2_TIERS)}"
            )
        return self


class AcceptanceVerdict(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    criterion: str
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    evidence: str


class Verification(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    outcome: Outcome
    criteria: list[str] = Field(default_factory=list)
    verdicts: list[AcceptanceVerdict] = Field(default_factory=list)
    unresolved_finding_ids: list[str] = Field(default_factory=list)

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
