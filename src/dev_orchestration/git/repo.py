"""Git operations. Every call uses an argument array; none reach a remote."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_orchestration.git import guards

GIT_TIMEOUT_SECONDS = 120

# run_git is public and takes unrestricted *args, so any caller could in
# principle ask it to push or merge. This is the runtime enforcement of
# that prohibition: it works even against a verb built dynamically at call
# time (e.g. "pu" + "sh"), which a static source scan cannot see. There is
# no legitimate call in this framework to either verb, so there is nothing
# valid to break.
_PROHIBITED_VERBS = {
    "push": guards.push,
    "merge": guards.merge,
}


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""


class DirtyWorktreeError(RuntimeError):
    """The base repository has uncommitted changes."""


@dataclass(frozen=True)
class GitRepo:
    root: Path

    def run_git(self, *args: str) -> str:
        if args and args[0] in _PROHIBITED_VERBS:
            _PROHIBITED_VERBS[args[0]]()
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
        return proc.stdout.strip()

    def status_porcelain(self) -> str:
        return self.run_git("status", "--porcelain")

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
