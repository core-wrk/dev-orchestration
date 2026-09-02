"""The implemented stage path for each M2 tier."""

from dev_orchestration.domain.enums import Tier


class UnsupportedTierError(RuntimeError):
    """A tier without an M2 implementation was requested."""


class TierDowngradeError(RuntimeError):
    """An override tried to run under weaker controls than classification."""


TIER_STAGES: dict[Tier, tuple[str, ...]] = {
    Tier.TRIVIAL: (
        "classification",
        "execution",
        "validation",
        "implementation_review",
        "final_verification",
    ),
    Tier.STANDARD: (
        "classification",
        "planning",
        "plan_review",
        "reconciliation",
        "execution",
        "validation",
        "implementation_review",
        "final_verification",
    ),
    Tier.SUBSTANTIAL: (
        "classification",
        "planning",
        "plan_review",
        "reconciliation",
        "human_approval",
        "execution",
        "validation",
        "implementation_review",
        "final_verification",
    ),
}


def stages_for(tier: Tier) -> tuple[str, ...]:
    try:
        return TIER_STAGES[tier]
    except KeyError as exc:
        raise UnsupportedTierError(
            f"tier {tier!r} has no implemented path; implemented tiers are {sorted(TIER_STAGES)}"
        ) from exc
