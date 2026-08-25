import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import (
    DENIED_COMMAND_TOKENS,
    ProjectConfig,
    Scope,
    ValidationCommand,
    denied_tokens,
)
from dev_orchestration.domain.enums import Tier


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


@pytest.mark.parametrize("token", DENIED_COMMAND_TOKENS)
def test_each_denied_token_is_enforced(token):
    """Verify each token in DENIED_COMMAND_TOKENS is independently enforced.

    This test ensures that removing any single token from DENIED_COMMAND_TOKENS
    would cause a test failure, catching regressions in deny-list coverage.
    """
    cmd = f"npm run {token}"
    with pytest.raises(ValidationError):
        ValidationCommand(command=cmd)


@pytest.mark.parametrize("token", DENIED_COMMAND_TOKENS)
def test_each_denied_token_caught_as_substring(token):
    """Verify deny-list tokens are caught as substrings (e.g., predeploy, redeploy).

    Ensures that tokens appearing in lifecycle hooks and other variations
    are caught by substring matching, not just whole words.
    """
    cmd = f"npm run pre{token}"
    with pytest.raises(ValidationError):
        ValidationCommand(command=cmd)


@pytest.mark.parametrize("token", DENIED_COMMAND_TOKENS)
def test_denied_token_case_insensitive(token):
    """Verify deny-list tokens are matched case-insensitively.

    This test ensures that removing .lower() from denied_tokens would break,
    catching regressions in case-folding.
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
