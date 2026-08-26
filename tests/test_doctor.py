import dataclasses
import subprocess
import textwrap
from dataclasses import dataclass

import pytest

from dev_orchestration.adapters.base import AdapterStatus, FakeAdapter
from dev_orchestration.doctor import Check, render, run_checks


def test_render_marks_passing_and_failing_checks():
    output = render(
        [
            Check(name="git available", ok=True, detail="2.39.5"),
            Check(name="repository clean", ok=False, detail="3 uncommitted files"),
        ]
    )
    lines = output.splitlines()
    assert lines[0].startswith("✓ git available")
    assert lines[1].startswith("! repository clean")
    assert "3 uncommitted files" in lines[1]


def test_render_handles_an_empty_check_list():
    assert render([]) == ""


def test_check_is_immutable():
    check = Check(name="x", ok=True, detail="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        check.ok = False


# --- hermetic repository-check fixtures -------------------------------


def _run_git(*args: str, cwd) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(tmp_path):
    _run_git("init", "-q", cwd=tmp_path)
    _run_git("config", "user.email", "t@t.t", cwd=tmp_path)
    _run_git("config", "user.name", "T", cwd=tmp_path)
    return tmp_path


def _commit_all(tmp_path, message="init") -> None:
    _run_git("add", "-A", cwd=tmp_path)
    _run_git("commit", "-q", "-m", message, cwd=tmp_path)


VALID_PROJECT_YAML = textwrap.dedent(
    """\
    project:
      name: sample
      class: internal_utility
    scope:
      include:
        - "src/**"
      exclude:
        - "vendor/**"
    validation:
      lint:
        command: "ruff check ."
      test:
        command: "pytest"
    """
)

MALFORMED_PROJECT_YAML = textwrap.dedent(
    """\
    scope:
      include:
        - "src/**"
    """
)


def _by_name(checks: list[Check], name: str) -> Check:
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in checks]}")


# --- non-repository / clean-vs-dirty behaviour -------------------------


def test_cwd_outside_a_git_repository_fails_the_repository_check(tmp_path):
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "git repository")
    assert check.ok is False


def test_clean_repo_without_project_config_fails_and_names_init(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    repo_check = _by_name(checks, "git repository")
    assert repo_check.ok is True
    clean_check = _by_name(checks, "repository clean")
    assert clean_check.ok is True
    config_check = _by_name(checks, "project config")
    assert config_check.ok is False
    assert "dev-orch init" in config_check.detail


def test_uncommitted_file_fails_repository_clean_with_count_and_instruction(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    _commit_all(tmp_path)
    (tmp_path / "scratch.txt").write_text("uncommitted\n")

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    clean_check = _by_name(checks, "repository clean")
    assert clean_check.ok is False
    assert "1" in clean_check.detail
    assert "commit or stash" in clean_check.detail.lower()


def test_valid_project_config_reports_scope_and_each_validation_command(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(VALID_PROJECT_YAML)
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    config_check = _by_name(checks, "project config")
    assert config_check.ok is True
    assert "internal_utility" in config_check.detail

    scope_check = _by_name(checks, "scope fence")
    assert scope_check.ok is True

    lint_check = _by_name(checks, "validation:lint")
    assert lint_check.ok is True
    assert lint_check.detail == "ruff check ."

    test_check = _by_name(checks, "validation:test")
    assert test_check.ok is True
    assert test_check.detail == "pytest"


def test_malformed_project_yaml_fails_config_check_without_raising(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(MALFORMED_PROJECT_YAML)
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    config_check = _by_name(checks, "project config")
    assert config_check.ok is False
    assert "invalid .ai/project.yaml" in config_check.detail


# --- adapter injection ---------------------------------------------------


@dataclass
class _StubAdapter:
    """Minimal healthcheck-only test double; never shells out."""

    status: AdapterStatus

    def healthcheck(self) -> AdapterStatus:
        return self.status


def test_unavailable_codex_fails_codex_available_check(tmp_path):
    stub = _StubAdapter(
        status=AdapterStatus(name="codex", available=False, detail="codex not found")
    )
    checks = run_checks(tmp_path, codex=stub, claude=FakeAdapter())
    check = _by_name(checks, "codex available")
    assert check.ok is False


def test_codex_with_goals_feature_but_no_headless_entry_point_reports_it(tmp_path):
    stub = _StubAdapter(
        status=AdapterStatus(
            name="codex",
            available=True,
            detail="codex 1.0",
            capabilities={"goals_feature": True, "goal_headless": False},
        )
    )
    checks = run_checks(tmp_path, codex=stub, claude=FakeAdapter())
    check = _by_name(checks, "codex goal mode")
    assert check.ok is False
    assert "no headless entry point" in check.detail
