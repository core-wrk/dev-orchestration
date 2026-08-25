import subprocess
from pathlib import Path

import pytest

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.git.worktree import create_worktree, remove_worktree, worktree_path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(root)


def test_worktree_path_follows_the_documented_layout():
    path = worktree_path(Path("/wt"), "helmfast-OS", "20260825-013500_feature_x")
    assert path == Path("/wt/helmfast-OS/20260825-013500_feature_x")


def test_create_worktree_produces_an_isolated_checkout(repo, tmp_path):
    target = tmp_path / "wt" / "run1"
    branch = "ai/20260825-013500_feature_x"
    created = create_worktree(repo, target, branch)
    assert created.is_dir()
    assert (created / "README.md").read_text() == "hello\n"
    assert (repo.root / "README.md").exists()
    checked_out_branch = subprocess.run(
        ["git", "-C", str(created), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert checked_out_branch == branch


def test_removing_a_worktree_leaves_the_base_repo_intact(repo, tmp_path):
    target = tmp_path / "wt" / "run2"
    create_worktree(repo, target, "ai/20260825-013500_feature_y")
    remove_worktree(repo, target)
    assert not target.exists()
    assert (repo.root / "README.md").exists()
