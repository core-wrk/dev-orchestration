import inspect
import shlex
import subprocess

import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import ProjectConfig, ValidationCommand
from dev_orchestration.domain.enums import Tier
from dev_orchestration.workflow.validation import (
    ValidationEnvironmentError,
    ValidationOutcome,
    all_passed,
    run_validations,
    validation_environment,
    validation_file_hashes,
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


def test_required_missing_broken_and_obstructed_targets_fail(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    base.mkdir()
    worktree.mkdir()
    with (
        pytest.raises(ValidationEnvironmentError),
        validation_environment(base, worktree, ("node_modules",), ("node_modules",)),
    ):
        pass

    (base / "node_modules").symlink_to(base / "missing", target_is_directory=True)
    with (
        pytest.raises(ValidationEnvironmentError),
        validation_environment(base, worktree, ("node_modules",), ("node_modules",)),
    ):
        pass
    (base / "node_modules").unlink()

    (base / "node_modules").mkdir()
    (worktree / "node_modules").write_text("obstructed")
    with (
        pytest.raises(ValidationEnvironmentError),
        validation_environment(base, worktree, ("node_modules",), ("node_modules",)),
    ):
        pass


def test_required_nested_link_and_interruption_cleanup(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "functions" / "node_modules").mkdir(parents=True)
    worktree.mkdir()
    with (
        pytest.raises(KeyboardInterrupt),
        validation_environment(
            base, worktree, ("functions/node_modules",), ("functions/node_modules",)
        ),
    ):
        assert (worktree / "functions" / "node_modules").is_dir()
        raise KeyboardInterrupt
    assert not (worktree / "functions" / "node_modules").exists()


def test_optional_missing_venv_does_not_block_node_validation_environment(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    base.mkdir()
    worktree.mkdir()
    with validation_environment(base, worktree):
        assert not (worktree / ".venv").exists()


def test_declared_validation_files_are_copied_then_removed(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "ops").mkdir(parents=True)
    worktree.mkdir()
    (base / "ops" / "template.pptx").write_bytes(b"synthetic")
    with validation_environment(base, worktree, validation_files=("ops/template.pptx",)):
        assert (worktree / "ops" / "template.pptx").read_bytes() == b"synthetic"
        outcome = run_validations(
            {
                "fixture": ValidationCommand(
                    command="test -f ops/template.pptx", required_for=[Tier.STANDARD]
                )
            },
            Tier.STANDARD,
            worktree,
        )
        assert outcome[0].passed is True
    assert not (worktree / "ops" / "template.pptx").exists()


def test_reliability_copy_cleanup_is_safe_for_replaced_parents_and_validation_results(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "ops").mkdir(parents=True)
    worktree.mkdir()
    source = base / "ops" / "template.pptx"
    source.write_bytes(b"synthetic source")
    spec = ValidationCommand(command="true", required_for=[Tier.STANDARD])

    with validation_environment(base, worktree, validation_files=("ops/template.pptx",)):
        assert run_validations({"fixture": spec}, Tier.STANDARD, worktree)[0].passed
    assert not (worktree / "ops" / "template.pptx").exists()

    with validation_environment(base, worktree, validation_files=("ops/template.pptx",)):
        failed = ValidationCommand(command="exit 3", required_for=[Tier.STANDARD])
        assert not run_validations({"fixture": failed}, Tier.STANDARD, worktree)[0].passed
    assert not (worktree / "ops" / "template.pptx").exists()

    replace_parent = f"rm -rf ops && ln -s {shlex.quote(str(base / 'ops'))} ops"
    attack = ValidationCommand(command=replace_parent, required_for=[Tier.STANDARD])
    with (
        pytest.raises(ValidationEnvironmentError, match="cleanup could not safely"),
        validation_environment(base, worktree, validation_files=("ops/template.pptx",)),
    ):
        assert run_validations({"fixture": attack}, Tier.STANDARD, worktree)[0].passed
    assert source.read_bytes() == b"synthetic source"


def test_reliability_copy_cleanup_continues_after_unsafe_removal(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "ops").mkdir(parents=True)
    (base / "node_modules" / "pkg").mkdir(parents=True)
    worktree.mkdir()
    unsafe_source = base / "ops" / "template.pptx"
    safe_source = base / "other" / "template.pptx"
    safe_source.parent.mkdir()
    unsafe_source.write_bytes(b"synthetic unsafe source")
    safe_source.write_bytes(b"synthetic safe source")
    replace_parent = f"rm -rf ops && ln -s {shlex.quote(str(base / 'ops'))} ops"
    attack = ValidationCommand(command=replace_parent, required_for=[Tier.STANDARD])

    with (
        pytest.raises(ValidationEnvironmentError, match="cleanup could not safely"),
        validation_environment(
            base,
            worktree,
            link_paths=("node_modules",),
            validation_files=("ops/template.pptx", "other/template.pptx"),
        ),
    ):
        assert run_validations({"fixture": attack}, Tier.STANDARD, worktree)[0].passed

    assert unsafe_source.read_bytes() == b"synthetic unsafe source"
    assert safe_source.read_bytes() == b"synthetic safe source"
    assert not (worktree / "other" / "template.pptx").exists()
    assert not (worktree / "node_modules").exists()


def test_declared_validation_file_refuses_to_overwrite_worktree_path(tmp_path):
    base = tmp_path / "base"
    worktree = tmp_path / "worktree"
    (base / "ops").mkdir(parents=True)
    (worktree / "ops").mkdir(parents=True)
    (base / "ops" / "template.pptx").write_bytes(b"source")
    (worktree / "ops" / "template.pptx").write_bytes(b"tracked")
    with (
        pytest.raises(ValidationEnvironmentError, match="already exists"),
        validation_environment(base, worktree, validation_files=("ops/template.pptx",)),
    ):
        pass
    assert (worktree / "ops" / "template.pptx").read_bytes() == b"tracked"


def test_validation_file_hashes_require_ignored_untracked_regular_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / ".gitignore").write_text("ops/template.pptx\n")
    (root / "ops").mkdir()
    (root / "ops" / "template.pptx").write_bytes(b"synthetic")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    from dev_orchestration.git.repo import GitRepo

    hashes = validation_file_hashes(GitRepo(root), ["ops/template.pptx"])
    assert len(hashes["ops/template.pptx"]) == 64
    (root / "ops" / "template.pptx").write_bytes(b"changed")
    assert validation_file_hashes(GitRepo(root), ["ops/template.pptx"]) != hashes

    (root / "ops" / "template.pptx").unlink()
    (root / "ops" / "template.pptx").symlink_to(root / ".gitignore")
    with pytest.raises(ValidationEnvironmentError, match="symlinks"):
        validation_file_hashes(GitRepo(root), ["ops/template.pptx"])
