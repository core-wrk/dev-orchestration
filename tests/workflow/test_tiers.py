import pytest

from dev_orchestration.context.assembler import STAGE_CONTRACTS
from dev_orchestration.domain.enums import Tier
from dev_orchestration.workflow.tiers import TIER_STAGES, UnsupportedTierError, stages_for


def test_trivial_skips_planning_and_plan_review():
    stages = stages_for(Tier.TRIVIAL)
    assert "planning" not in stages and "plan_review" not in stages and "execution" in stages


def test_standard_plans_and_reviews_before_execution():
    stages = stages_for(Tier.STANDARD)
    assert stages.index("planning") < stages.index("plan_review") < stages.index("execution")


def test_substantial_plans_reviews_and_can_pause_for_approval():
    stages = stages_for(Tier.SUBSTANTIAL)
    assert stages.index("planning") < stages.index("plan_review")
    assert stages.index("plan_review") < stages.index("human_approval")
    assert stages.index("human_approval") < stages.index("execution")


def test_both_tiers_validate_and_verify():
    for tier in (Tier.TRIVIAL, Tier.STANDARD, Tier.SUBSTANTIAL):
        stages = stages_for(tier)
        assert "validation" in stages and "final_verification" in stages


def test_unimplemented_tier_fails_loudly():
    with pytest.raises(UnsupportedTierError):
        stages_for(Tier.HIGH_RISK)


def test_every_stage_has_a_context_contract():
    for tier, stages in TIER_STAGES.items():
        for stage in stages:
            assert stage in STAGE_CONTRACTS, f"{tier}:{stage}"


def test_trivial_omits_implementation_review_but_keeps_verification():
    stages = stages_for(Tier.TRIVIAL)
    assert "implementation_review" not in stages
    assert "validation" in stages and "final_verification" in stages


def test_standard_and_substantial_keep_implementation_review():
    for tier in (Tier.STANDARD, Tier.SUBSTANTIAL):
        assert "implementation_review" in stages_for(tier)
