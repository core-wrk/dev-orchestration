import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import (
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


def test_denied_tokens_reports_which_word_tripped():
    assert denied_tokens("npm run deploy:prod") == {"deploy"}
    assert denied_tokens("npm run build") == set()


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
