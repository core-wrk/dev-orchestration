"""Git operations. Every call uses an argument array; none reach a remote."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_orchestration.git import guards

GIT_TIMEOUT_SECONDS = 120


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""


class DirtyWorktreeError(RuntimeError):
    """The base repository has uncommitted changes."""


@dataclass(frozen=True)
class GitRepo:
    root: Path

    def run_git(self, *args: str, strip: bool = True) -> str:
        # run_git is public and takes unrestricted *args, so any caller
        # could in principle ask it to push or merge. The prohibited-verb
        # table lives in guards.py, not here, so that module stays the
        # sole home of these literals and repo.py needs no exemption from
        # the invocation scan in tests/git/test_guards.py.
        if args:
            guards.reject_prohibited_verb(args[0])
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip() if strip else proc.stdout

    def status_porcelain(self) -> str:
        # `git status --porcelain` lines are `XY<space>PATH`, where either
        # status character may itself be a space (e.g. an unstaged
        # modification is ` M path`). run_git's default .strip() would eat
        # that leading space off the *first* line only, truncating the
        # first character of its path once callers slice past the fixed
        # 3-character prefix. Only the trailing newline is ours to remove.
        return self.run_git("status", "--porcelain", strip=False).rstrip("\n")

    def ensure_clean(self) -> None:
        status = self.status_porcelain()
        if status:
            files = "\n  ".join(line[3:] for line in status.splitlines())
            raise DirtyWorktreeError(
                f"{self.root} has uncommitted changes:\n  {files}\n"
                "Commit or stash them before starting a run. "
                "dev-orch never modifies uncommitted work."
            )

    def current_branch(self) -> str:
        return self.run_git("rev-parse", "--abbrev-ref", "HEAD")

    def current_commit(self) -> str:
        return self.run_git("rev-parse", "HEAD")

    def create_branch(self, name: str) -> None:
        self.run_git("checkout", "-q", "-b", name)

    def commit(self, message: str, paths: list[str]) -> str:
        self.run_git("add", "--", *paths)
        self.run_git("commit", "-q", "-m", message, "--", *paths)
        return self.current_commit()

    def changed_files(self, base_ref: str) -> list[str]:
        output = self.run_git("diff", "--name-only", base_ref, "HEAD")
        return [line for line in output.splitlines() if line]


def discover_repo(start: Path) -> GitRepo:
    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise GitCommandError(f"{start} is not inside a git repository")
    return GitRepo(Path(proc.stdout.strip()))
