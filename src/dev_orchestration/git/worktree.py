"""Isolated worktrees. Agents only ever run inside one of these."""

from pathlib import Path

from dev_orchestration.git.repo import GitRepo


def worktree_path(worktree_root: Path, repo_name: str, run_id: str) -> Path:
    return worktree_root / repo_name / run_id


def create_worktree(repo: GitRepo, path: Path, branch: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    repo.run_git("worktree", "add", "-q", "-b", branch, str(path))
    return path


def remove_worktree(repo: GitRepo, path: Path) -> None:
    repo.run_git("worktree", "remove", "--force", str(path))
