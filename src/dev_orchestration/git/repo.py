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
        guards.reject_disallowed_invocation(args)
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
        paths = self.status_paths()
        if paths:
            files = "\n  ".join(paths)
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
        return self.change_inventory(base_ref)

    def status_paths(self) -> list[str]:
        """Return all paths represented by porcelain status, including renames."""
        output = self.run_git("status", "--porcelain=v1", "-z", strip=False)
        paths: list[str] = []
        for path in _parse_porcelain_paths(output):
            target = self.root / path.rstrip("/")
            if path.endswith("/") and target.is_dir():
                paths.extend(
                    child.relative_to(self.root).as_posix()
                    for child in target.rglob("*")
                    if child.is_file()
                )
            else:
                paths.append(path)
        return sorted(set(paths))

    def change_inventory(self, base_ref: str) -> list[str]:
        """Inventory committed, staged, unstaged, deleted, renamed and untracked paths."""
        committed_or_index = self.run_git("diff", "--name-status", "-z", base_ref, strip=False)
        paths = set(_parse_name_status_paths(committed_or_index))
        paths.update(self.status_paths())
        return sorted(paths)

    def change_diff(self, base_ref: str) -> str:
        """Return the diff plus the contents of untracked files."""
        diff = self.run_git("diff", base_ref, strip=False)
        untracked = []
        status = self.status_paths()
        for path in status:
            target = self.root / path
            if target.is_file() and not self._path_is_tracked(path):
                untracked.append(
                    f"\n--- untracked: {path} ---\n{target.read_text(encoding='utf-8')}\n"
                )
        return diff + "".join(untracked)

    def _path_is_tracked(self, path: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "--error-unmatch", "--", path],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        return proc.returncode == 0

    def is_ancestor(self, ancestor: str, commit: str | None = None) -> bool:
        target = commit or self.current_commit()
        proc = subprocess.run(
            ["git", "-C", str(self.root), "merge-base", "--is-ancestor", ancestor, target],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        return proc.returncode == 0

    def commit_snapshot(self, message: str, paths: list[str]) -> str:
        """Stage the verified snapshot and create a local commit, even if empty."""
        del paths
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "--allow-empty", "-m", message)
        return self.current_commit()


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


def _parse_name_status_paths(output: str) -> list[str]:
    parts = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if "\t" in status:
            status, path = status.split("\t", 1)
            paths.append(path)
            continue
        if status[0] in {"R", "C"} and index + 1 < len(parts):
            paths.extend([parts[index], parts[index + 1]])
            index += 2
        elif index < len(parts):
            paths.append(parts[index])
            index += 1
    return [path for path in paths if path]


def _parse_porcelain_paths(output: str) -> list[str]:
    parts = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            continue
        status = entry[:2]
        paths.append(entry[3:])
        if status[0] in {"R", "C"} and index < len(parts):
            paths.append(parts[index])
            index += 1
    return [path for path in paths if path]
