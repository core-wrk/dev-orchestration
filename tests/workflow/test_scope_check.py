import subprocess

import pytest

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    check_diff_against_fence,
    enforce_fence,
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
