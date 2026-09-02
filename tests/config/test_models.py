import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import (
    DENIED_COMMAND_TOKENS,
    ApprovalPolicy,
    ContextPolicy,
    ProjectConfig,
    Scope,
    ValidationCommand,
    denied_tokens,
)
from dev_orchestration.domain.enums import Tier

# Independent literal declaration, decoupled from source.
# If someone deletes a token from DENIED_COMMAND_TOKENS, the pinning test
# fails AND the parametrized tests still have all expected cases.
EXPECTED_DENIED_TOKENS = [
    "deploy",
    "wrangler",
    "publish",
    "push",
    "merge",
    "release",
]


def test_deny_list_contents_are_pinned():
    """Verify the deny-list has exactly the expected tokens.

    This guards against accidental modifications to DENIED_COMMAND_TOKENS.
    If a token is added or removed from the source, this test fails.
    """
    assert DENIED_COMMAND_TOKENS == frozenset(EXPECTED_DENIED_TOKENS)


def test_safe_command_is_accepted():
    cmd = ValidationCommand(command="npm run build", required_for=[Tier.STANDARD])
    assert cmd.command == "npm run build"


@pytest.mark.parametrize(
    "command",
    [
        "npm run deploy",
        "wrangler deploy",
        "npm run deploy:prod",
        "git push origin main",
        "npm publish",
        "gh release create",
    ],
)
def test_dangerous_commands_are_rejected(command):
    with pytest.raises(ValidationError):
        ValidationCommand(command=command)


@pytest.mark.parametrize("token", EXPECTED_DENIED_TOKENS)
def test_each_denied_token_is_enforced(token):
    """Verify each expected token is rejected in a basic command.

    This test uses EXPECTED_DENIED_TOKENS (independent literal) so that
    if someone deletes a token from the source, we still test it here.
    The pinning test (test_deny_list_contents_are_pinned) catches the deletion.
    """
    cmd = f"npm run {token}"
    with pytest.raises(ValidationError):
        ValidationCommand(command=cmd)


@pytest.mark.parametrize("token", EXPECTED_DENIED_TOKENS)
def test_each_denied_token_caught_as_substring(token):
    """Verify deny-list tokens are caught as substrings (e.g., predeploy, redeploy).

    This test uses EXPECTED_DENIED_TOKENS (independent literal) so that
    if someone deletes a token from the source, we still test it here.
    The pinning test (test_deny_list_contents_are_pinned) catches the deletion.
    """
    cmd = f"npm run pre{token}"
    with pytest.raises(ValidationError):
        ValidationCommand(command=cmd)


@pytest.mark.parametrize("token", EXPECTED_DENIED_TOKENS)
def test_denied_token_case_insensitive(token):
    """Verify deny-list tokens are matched case-insensitively.

    This test uses EXPECTED_DENIED_TOKENS (independent literal) and verifies
    that uppercase variants are still caught. It ensures that removing .lower()
    from denied_tokens would break, catching regressions in case-folding.
    """
    cmd = f"npm run {token.upper()}"
    with pytest.raises(ValidationError):
        ValidationCommand(command=cmd)


def test_denied_tokens_reports_which_word_tripped():
    assert denied_tokens("npm run deploy:prod") == {"deploy"}
    assert denied_tokens("npm run build") == set()


def test_denied_tokens_catches_substrings():
    """Verify deny-list tokens are caught as substrings within words."""
    assert denied_tokens("npm run predeploy") == {"deploy"}
    assert denied_tokens("npm run redeploy") == {"deploy"}
    assert denied_tokens("npm run deployment") == {"deploy"}
    assert denied_tokens("npm run redeployment") == {"deploy"}
    assert "deploy" in denied_tokens("npm run predeploy")


def test_repo_config_cannot_smuggle_a_denied_command():
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {
                "project": {"name": "helmfast-site", "class": "marketing_website"},
                "validation": {"build": {"command": "npm run deploy"}},
            }
        )


def test_scope_defaults_to_whole_repo():
    assert Scope().include == ["**"]
    assert Scope().exclude == []


def test_context_policy_defaults():
    policy = ContextPolicy()
    assert policy.persistent == ["AGENTS.md", ".ai/context.md"]
    assert policy.on_demand == {}
    assert policy.include_prior_artifacts == "relevant_only"
    assert policy.conflict_policy == "fail_closed"
    assert policy.persistent_budget_bytes == 8192


def test_context_policy_conflict_default_is_fail_closed():
    # fail_closed is a safety choice, not a preference: an agent that cannot
    # reconcile conflicting context must stop, not guess. Assert the literal
    # value by name so a silent flip to "escalate" is caught here.
    assert ContextPolicy().conflict_policy == "fail_closed"


def test_project_config_gains_context_policy_by_default():
    config = ProjectConfig.model_validate(
        {"project": {"name": "helmfast-site", "class": "marketing_website"}}
    )
    assert isinstance(config.context, ContextPolicy)
    assert config.context.conflict_policy == "fail_closed"


def test_approval_policy_defaults_to_gate_high_risk_only():
    config = ProjectConfig.model_validate(
        {"project": {"name": "dev-orchestration", "class": "internal_operating_system"}}
    )
    assert config.approval == ApprovalPolicy(substantial=False, high_risk=True)


def test_approval_policy_round_trips_through_config_dump():
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "dev-orchestration", "class": "internal_operating_system"},
            "approval": {"substantial": True, "high_risk": False},
        }
    )
    restored = ProjectConfig.model_validate(config.model_dump(mode="json"))
    assert restored.approval == config.approval


def test_context_policy_round_trips_through_config_dump():
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "helmfast-site", "class": "marketing_website"},
            "context": {"conflict_policy": "escalate", "persistent_budget_bytes": 4096},
        }
    )
    dumped = config.model_dump(by_alias=True, mode="json")
    restored = ProjectConfig.model_validate(dumped)
    assert restored.context == config.context
    assert restored.context.conflict_policy == "escalate"
    assert restored.context.persistent_budget_bytes == 4096
