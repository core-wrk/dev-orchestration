import subprocess

import pytest

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    check_diff_against_fence,
    check_ignored_writes,
    enforce_fence,
    enforce_ignored_writes,
)

FENCE = ScopeFence(include=["src/"], exclude=["secrets/"])


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(tmp_path)


def test_committed_inside_and_outside_paths_are_checked(repo):
    base = repo.current_commit()
    (repo.root / "src" / "app.py").write_text("x = 2\n")
    (repo.root / "secrets").mkdir()
    (repo.root / "secrets" / "keys.txt").write_text("token\n")
    assert check_diff_against_fence(repo, base, FENCE) == ["secrets/keys.txt"]


@pytest.mark.parametrize("state", ["untracked", "staged", "unstaged"])
def test_uncommitted_staged_and_untracked_out_of_scope_paths_fail(repo, state):
    base = repo.current_commit()
    target = repo.root / f"outside-{state}.txt"
    target.write_text("bad\n")
    if state == "staged":
        subprocess.run(["git", "-C", str(repo.root), "add", str(target)], check=True)
    elif state == "unstaged":
        subprocess.run(["git", "-C", str(repo.root), "add", "-A"], check=True)
        target.write_text("bad2\n")
    assert check_diff_against_fence(repo, base, FENCE) == [target.name]


def test_enforcement_names_every_offending_file(repo):
    base = repo.current_commit()
    (repo.root / "vendor").mkdir()
    (repo.root / "vendor" / "lib.py").write_text("y = 1\n")
    with pytest.raises(ScopeViolation) as error:
        enforce_fence(repo, base, FENCE)
    assert "vendor/lib.py" in str(error.value)


def test_ignored_file_modified_outside_the_fence_is_a_violation(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    payload = repo.root / "dist" / "payload.sh"
    payload.write_text("before\n")
    before = repo.ignored_paths()
    payload.write_text("after\n")
    assert check_ignored_writes(repo, before, FENCE) == ["dist/payload.sh"]


def test_ignored_file_present_before_the_stage_is_not_attributed(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "cache.bin").write_text("pre-existing\n")
    before = repo.ignored_paths()
    assert check_ignored_writes(repo, before, FENCE) == []


def test_ignored_file_deleted_and_type_change_are_attributed(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    payload = repo.root / "dist" / "payload.sh"
    payload.write_text("before\n")
    before = repo.ignored_paths()
    payload.unlink()
    with pytest.raises(ScopeViolation):
        enforce_ignored_writes(repo, before, FENCE)
    payload.mkdir()
    (payload / "child").write_text("after\n")
    assert check_ignored_writes(repo, before, FENCE) == [
        "dist/payload.sh",
        "dist/payload.sh/child",
    ]


def test_ignored_in_scope_write_is_allowed(repo):
    (repo.root / ".gitignore").write_text("*.log\n")
    before = repo.ignored_paths()
    (repo.root / "src" / "build.log").write_text("in scope\n")
    enforce_ignored_writes(repo, before, FENCE)


def test_enforcement_explains_why_an_ignored_write_is_invisible(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    before = repo.ignored_paths()
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    with pytest.raises(ScopeViolation) as error:
        enforce_ignored_writes(repo, before, FENCE)
    assert "dist/payload.sh" in str(error.value)
    assert "ignored" in str(error.value).lower()
