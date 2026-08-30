import pytest

from dev_orchestration.config.resolver import (
    PROTECTED_DEFAULTS,
    ProtectedRuleViolation,
    resolve_policy,
)


def test_later_layer_wins_for_ordinary_keys():
    resolved = resolve_policy(
        [{"workflow": {"max_remediation_cycles": 2}}, {"workflow": {"max_remediation_cycles": 5}}]
    )
    assert resolved["workflow.max_remediation_cycles"] == 5


def test_stricter_requirement_wins_regardless_of_order():
    profile = {"planning": {"plan_review_required": True}}
    repo = {"planning": {"plan_review_required": False}}
    assert resolve_policy([profile, repo])["planning.plan_review_required"] is True
    assert resolve_policy([repo, profile])["planning.plan_review_required"] is True


def test_repo_cannot_weaken_protected_rules():
    rogue = {"protected": {"autonomous_push": True, "autonomous_deploy": True}}
    with pytest.raises(ProtectedRuleViolation):
        resolve_policy([rogue])


def test_a_layer_that_restates_a_protected_rule_as_false_is_accepted():
    resolved = resolve_policy([{"protected": {"autonomous_push": False}}])
    assert resolved["protected.autonomous_push"] is False


def test_a_later_layer_cannot_reopen_a_protected_rule():
    with pytest.raises(ProtectedRuleViolation):
        resolve_policy(
            [
                {"protected": {"autonomous_deploy": False}},
                {"protected": {"autonomous_deploy": True}},
            ]
        )


def test_protected_defaults_are_all_false():
    assert set(PROTECTED_DEFAULTS.values()) == {False}


def test_nested_keys_flatten_to_dotted_paths():
    resolved = resolve_policy([{"git": {"isolated_worktree": True}}])
    assert resolved["git.isolated_worktree"] is True
