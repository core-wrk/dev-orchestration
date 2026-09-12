import dataclasses
import subprocess
import textwrap
from dataclasses import dataclass

import pytest

import dev_orchestration.doctor as doctor_module
from dev_orchestration.adapters.base import AdapterStatus, FakeAdapter
from dev_orchestration.config.models import ProjectConfig
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
    assert "git init" in check.detail


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


def test_unstaged_dotfile_modification_keeps_its_leading_character_in_doctor_detail(tmp_path):
    # `git status --porcelain` renders an unstaged modification as
    # " M path" -- a leading space in the status field. Stripping the
    # whole porcelain block eats that space off the *first* line only,
    # truncating a leading dotfile path (".env.example" -> "env.example").
    # ".env.example" is the real-world case that surfaced this bug.
    _init_repo(tmp_path)
    (tmp_path / ".env.example").write_text("KEY=1\n")
    _commit_all(tmp_path)
    (tmp_path / ".env.example").write_text("KEY=2\n")

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    clean_check = _by_name(checks, "repository clean")
    assert clean_check.ok is False
    assert ".env.example" in clean_check.detail
    assert "env.example" not in clean_check.detail.replace(".env.example", "")


def test_multiple_dirty_files_all_named_in_doctor_detail_when_first_is_unstaged(tmp_path):
    # The truncation bug only ever hit the first porcelain line; a
    # single-file case cannot show the fix generalizes.
    _init_repo(tmp_path)
    (tmp_path / ".env.example").write_text("KEY=1\n")
    _commit_all(tmp_path)
    (tmp_path / ".env.example").write_text("KEY=2\n")
    (tmp_path / "second.txt").write_text("second\n")

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    clean_check = _by_name(checks, "repository clean")
    assert clean_check.ok is False
    assert "2" in clean_check.detail
    assert ".env.example" in clean_check.detail
    assert "second.txt" in clean_check.detail
    assert "env.example" not in clean_check.detail.replace(".env.example", "")


def test_valid_project_config_reports_scope_and_each_validation_command(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(VALID_PROJECT_YAML)
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    config_check = _by_name(checks, "project config")
    assert config_check.ok is True
    assert "internal_utility" in config_check.detail

    # VALID_PROJECT_YAML declares include=["src/**"] and exclude=["vendor/**"].
    # Assert the detail names include/exclude in the correct positions, so a
    # mutation that swaps them (or renders both lists as a single unordered
    # blob) is caught rather than merely "both strings appear somewhere".
    scope_check = _by_name(checks, "scope fence")
    assert scope_check.ok is True
    assert scope_check.detail == "include=['src/**'] exclude=['vendor/**']"

    lint_check = _by_name(checks, "validation:lint")
    assert lint_check.ok is True
    assert lint_check.detail == "ruff check ."

    test_check = _by_name(checks, "validation:test")
    assert test_check.ok is True
    assert test_check.detail == "pytest"


def test_agents_md_present_reports_ok_with_no_detail(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(VALID_PROJECT_YAML)
    (tmp_path / "AGENTS.md").write_text("# AGENTS\n")
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    agents_check = _by_name(checks, "AGENTS.md")
    assert agents_check.ok is True
    assert agents_check.detail == ""


def test_agents_md_missing_reports_next_action(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(VALID_PROJECT_YAML)
    _commit_all(tmp_path)

    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())

    agents_check = _by_name(checks, "AGENTS.md")
    assert agents_check.ok is False
    assert "dev-orch init" in agents_check.detail


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
    # No "codex goal mode" check should be appended at all when codex itself
    # is unavailable — there is nothing to report a goal-mode capability of.
    assert "codex goal mode" not in [c.name for c in checks]


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


def test_codex_without_goals_feature_states_the_exec_fallback(tmp_path):
    stub = _StubAdapter(
        status=AdapterStatus(
            name="codex",
            available=True,
            detail="codex 1.0",
            capabilities={"goals_feature": False, "goal_headless": False},
        )
    )
    checks = run_checks(tmp_path, codex=stub, claude=FakeAdapter())
    check = _by_name(checks, "codex goal mode")
    assert check.ok is False
    assert "codex exec" in check.detail
    # No user action is possible here (goal mode simply isn't present on this
    # build); the detail states the consequence, not a fabricated fix.
    assert "no headless goal mode" in check.detail


# --- environment checks --------------------------------------------------


def test_macos_check_passes_when_platform_reports_darwin(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.platform, "system", lambda: "Darwin")
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "macOS")
    assert check.ok is True


def test_macos_check_fails_on_a_non_darwin_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.platform, "system", lambda: "Linux")
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "macOS")
    assert check.ok is False


def test_python_version_check_passes_for_a_supported_version(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.sys, "version_info", (3, 14, 0, "final", 0))
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "python >= 3.11")
    assert check.ok is True


def test_python_version_check_fails_for_an_unsupported_version(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.sys, "version_info", (3, 9, 0, "final", 0))
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "python >= 3.11")
    assert check.ok is False


def test_git_available_check_passes_when_git_is_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/usr/bin/git")
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "git available")
    assert check.ok is True


def test_git_available_check_fails_and_names_install_when_git_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)
    checks = run_checks(tmp_path, codex=FakeAdapter(), claude=FakeAdapter())
    check = _by_name(checks, "git available")
    assert check.ok is False
    assert check.detail == "install git"


def test_git_version_reports_install_git_when_git_is_absent(monkeypatch):
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)
    assert doctor_module._git_version() == "install git"


def test_proportionality_flags_trivial_without_validation_and_inert_controls():
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "class": "internal_utility"},
            "validation": {"unit": {"command": "true", "required_for": ["standard"]}},
            "profiles": {
                "available": ["p"],
                "definitions": {
                    "p": {"controls": {"human_approval": True, "plan_review_required": True}}
                },
            },
        }
    )
    checks = {check.name: check for check in doctor_module._proportionality_checks(config)}
    assert checks["validation covers trivial"].ok is False
    assert checks["profile controls enforced"].ok is False
    assert "plan_review_required" in checks["profile controls enforced"].detail
    assert "human_approval" not in checks["profile controls enforced"].detail


def test_proportionality_passes_when_trivial_is_validated_and_controls_are_live():
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "class": "internal_utility"},
            "validation": {"unit": {"command": "true", "required_for": ["trivial", "standard"]}},
            "profiles": {
                "available": ["p"],
                "definitions": {"p": {"controls": {"human_approval": True}}},
            },
        }
    )
    assert all(check.ok for check in doctor_module._proportionality_checks(config))
