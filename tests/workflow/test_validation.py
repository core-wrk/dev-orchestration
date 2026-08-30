import inspect

import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import ProjectConfig, ValidationCommand
from dev_orchestration.domain.enums import Tier
from dev_orchestration.workflow.validation import ValidationOutcome, all_passed, run_validations


def test_passing_and_failing_commands_record_exit_codes(tmp_path):
    commands = {
        "ok": ValidationCommand(command="true", required_for=[Tier.STANDARD]),
        "bad": ValidationCommand(command="exit 3", required_for=[Tier.STANDARD]),
    }
    outcomes = run_validations(commands, Tier.STANDARD, tmp_path)
    assert [outcome.passed for outcome in outcomes] == [True, False]
    assert outcomes[1].exit_code == 3
    assert not all_passed(outcomes)


def test_commands_not_required_for_tier_do_not_run(tmp_path):
    commands = {"high": ValidationCommand(command="exit 1", required_for=[Tier.HIGH_RISK])}
    assert run_validations(commands, Tier.STANDARD, tmp_path) == []


def test_output_timeout_and_cwd_are_recorded(tmp_path):
    (tmp_path / "marker.txt").write_text("here")
    output = run_validations(
        {"unit": ValidationCommand(command="ls marker.txt", required_for=[Tier.STANDARD])},
        Tier.STANDARD,
        tmp_path,
    )
    assert output[0].passed and "marker.txt" in output[0].stdout_tail
    timeout = run_validations(
        {
            "slow": ValidationCommand(
                command="sleep 30", required_for=[Tier.STANDARD], timeout_seconds=1
            )
        },
        Tier.STANDARD,
        tmp_path,
    )
    assert timeout[0].passed is False and "timed out" in timeout[0].stderr_tail


def test_validation_has_no_agent_output_parameter():
    assert set(inspect.signature(run_validations).parameters) == {"commands", "tier", "cwd"}


def test_denied_commands_fail_at_config_load():
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {
                "project": {"name": "x", "class": "internal_utility"},
                "validation": {"x": {"command": "npm run deploy"}},
            }
        )


def test_outcome_is_immutable():
    outcome = ValidationOutcome(
        name="x", command="true", exit_code=0, passed=True, stdout_tail="", stderr_tail=""
    )
    with pytest.raises(ValidationError):
        outcome.passed = False
