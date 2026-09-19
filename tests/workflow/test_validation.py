import inspect

import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import ProjectConfig, ValidationCommand
from dev_orchestration.domain.enums import Tier
from dev_orchestration.workflow.validation import (
    ValidationOutcome,
    all_passed,
    run_validations,
    validation_environment,
)


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


def test_validation_environment_temporarily_links_base_venv(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / ".venv" / "bin").mkdir(parents=True)
    worktree.mkdir()

    with validation_environment(base, worktree):
        assert (worktree / ".venv").is_symlink()
        assert (worktree / ".venv").resolve() == (base / ".venv").resolve()

    assert not (worktree / ".venv").exists()


def test_validation_environment_preserves_existing_worktree_venv(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / ".venv").mkdir(parents=True)
    (worktree / ".venv").mkdir(parents=True)

    with validation_environment(base, worktree):
        assert (worktree / ".venv").is_dir()
        assert not (worktree / ".venv").is_symlink()

    assert (worktree / ".venv").is_dir()


def test_validation_environment_links_configured_paths_including_nested(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "node_modules" / "pkg").mkdir(parents=True)
    (base / "functions" / "node_modules" / "pkg").mkdir(parents=True)
    (worktree / "functions").mkdir(parents=True)

    link_paths = ("node_modules", "functions/node_modules")
    with validation_environment(base, worktree, link_paths):
        assert (worktree / "node_modules").resolve() == (base / "node_modules").resolve()
        assert (worktree / "functions" / "node_modules").resolve() == (
            base / "functions" / "node_modules"
        ).resolve()

    assert not (worktree / "node_modules").exists()
    assert not (worktree / "functions" / "node_modules").exists()


def test_validation_environment_skips_paths_missing_from_base(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    base.mkdir()
    worktree.mkdir()

    with validation_environment(base, worktree, ("node_modules",)):
        assert not (worktree / "node_modules").exists()
