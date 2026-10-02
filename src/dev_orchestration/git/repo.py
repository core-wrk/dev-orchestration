"""Git operations. Every call uses an argument array; none reach a remote."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dev_orchestration.git import guards

GIT_TIMEOUT_SECONDS = 120


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""


class DirtyWorktreeError(RuntimeError):
    """The base repository has uncommitted changes."""


class EmptySnapshotError(RuntimeError):
    """A run reached its final commit with no change to record."""


@dataclass(frozen=True)
class _IgnoredEntry:
    kind: str
    mode: int
    digest: str | None = None
    link_target: str | None = None
    content: bytes | None = None

    @property
    def signature(self) -> tuple[str, int, str | None, str | None]:
        return self.kind, self.mode, self.digest, self.link_target


class IgnoredPathInventory(list[str]):
    """A list-compatible ignored-path inventory with content snapshots."""

    def __init__(self, paths: list[str], states: dict[str, _IgnoredEntry]) -> None:
        super().__init__(paths)
        self.states = states


MAX_IGNORED_FILE_BYTES = 64 * 1024 * 1024


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

    def remote_url(self) -> str:
        """Read the origin identity without changing any repository configuration."""
        return self.run_git("config", "--get", "remote.origin.url")

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

    def change_inventory(
        self,
        base_ref: str,
        ignored_before: list[str] | Mapping[str, _IgnoredEntry] | None = None,
    ) -> list[str]:
        """Inventory visible changes and, when supplied, ignored mutations."""
        committed_or_index = self.run_git("diff", "--name-status", "-z", base_ref, strip=False)
        paths = set(_parse_name_status_paths(committed_or_index))
        paths.update(self.status_paths())
        if ignored_before is not None:
            paths.update(self.ignored_delta(ignored_before))
        return sorted(paths)

    def change_diff(self, base_ref: str) -> str:
        """Return the diff plus the contents of untracked files."""
        diff = self.run_git("diff", base_ref, strip=False)
        untracked = []
        status = self.status_paths()
        for path in status:
            target = self.root / path
            if target.is_file() and not self._path_is_tracked(path):
                raw = target.read_bytes()
                try:
                    content = raw.decode("utf-8")
                    untracked.append(f"\n--- untracked: {path} ---\n{content}\n")
                except UnicodeDecodeError:
                    digest = hashlib.sha256(raw).hexdigest()
                    untracked.append(
                        f"\n--- untracked binary: {path} ({len(raw)} bytes, sha256 {digest}) ---\n"
                    )
        return diff + "".join(untracked)

    def change_patch(self, base_ref: str) -> str:
        """Return a binary-safe patch of every visible change, untracked files included.

        Built in a throwaway index so the checkout's own index is never touched.
        """
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            # The temporary index starts empty, so `add -A` snapshots the whole tree
            # and the cached diff against base_ref yields exactly the run's changes.
            for args in (["add", "-A"],):
                guards.reject_disallowed_invocation(tuple(args))
                step = subprocess.run(
                    ["git", "-C", str(self.root), *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT_SECONDS,
                    check=False,
                )
                if step.returncode != 0:
                    raise GitCommandError(f"git {args[0]} failed: {step.stderr.strip()}")
            proc = subprocess.run(
                ["git", "-C", str(self.root), "diff", "--cached", "--binary", base_ref],
                env=env,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        if proc.returncode != 0:
            raise GitCommandError(f"git diff --cached failed: {proc.stderr.strip()}")
        return proc.stdout

    def apply_patch(self, patch: str) -> None:
        """Apply a patch from change_patch to the working tree only; all or nothing."""
        guards.reject_disallowed_invocation(("apply", "--binary"))
        proc = subprocess.run(
            ["git", "-C", str(self.root), "apply", "--binary"],
            input=patch,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise GitCommandError(f"git apply failed: {proc.stderr.strip()}")

    def _path_is_tracked(self, path: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "--error-unmatch", "--", path],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        return proc.returncode == 0

    def is_tracked(self, path: str) -> bool:
        """Return whether a path is part of the repository index."""
        return self._path_is_tracked(path)

    def ignored_paths(self) -> IgnoredPathInventory:
        """Inventory every currently ignored file and snapshot its contents.

        Git's status output omits ignored files and its directory-oriented
        ignored mode collapses whole trees. ``ls-files`` gives us one path per
        file; the attached snapshot makes the result content-aware, so an
        overwrite, deletion, mode change, symlink replacement, or type change
        cannot hide behind a pre-existing ignored pathname.
        """
        output = self.run_git("ls-files", "-o", "-i", "--exclude-standard", "-z", strip=False)
        paths = sorted({path for path in output.split("\0") if path})
        return IgnoredPathInventory(paths, self._capture_ignored(paths))

    def ignored_delta(
        self,
        before: list[str] | Mapping[str, _IgnoredEntry],
    ) -> list[str]:
        """Return ignored paths whose filesystem state changed since ``before``."""
        before_states = _states_from_inventory(before)
        current = self.ignored_paths()
        paths = set(before_states) | set(current.states)
        return sorted(
            path
            for path in paths
            if _state_signature(before_states.get(path))
            != _state_signature(current.states.get(path))
        )

    def restore_ignored_snapshot(
        self,
        before: list[str] | Mapping[str, _IgnoredEntry],
    ) -> None:
        """Restore a stage's ignored state inside this isolated worktree.

        This is intentionally exact-path cleanup. It never operates on a
        broad directory and is used only for the disposable run worktree, so
        validation caches and rejected agent writes cannot become invisible
        terminal residue.
        """
        before_states = _states_from_inventory(before)
        current = self.ignored_paths()
        for path in sorted(set(before_states) | set(current.states), key=_path_depth, reverse=True):
            target = self.root / path
            _ensure_relative_path(self.root, target)
            wanted = before_states.get(path)
            if wanted is None:
                _remove_path(target)
            else:
                _remove_path(target)
                _restore_entry(target, wanted)

    def _capture_ignored(self, paths: list[str]) -> dict[str, _IgnoredEntry]:
        states: dict[str, _IgnoredEntry] = {}
        for path in paths:
            target = self.root / path
            try:
                stat_result = target.lstat()
            except FileNotFoundError:
                continue
            mode = stat_result.st_mode
            permissions = mode & 0o7777
            if os.path.islink(target):
                link_target = os.readlink(target)
                digest = hashlib.sha256(link_target.encode("utf-8")).hexdigest()
                states[path] = _IgnoredEntry("symlink", permissions, digest, link_target)
            elif os.path.isfile(target):
                if stat_result.st_size > MAX_IGNORED_FILE_BYTES:
                    raise GitCommandError(
                        f"cannot safely inventory ignored file {path}: "
                        f"{stat_result.st_size} bytes exceeds the {MAX_IGNORED_FILE_BYTES}-byte limit"
                    )
                try:
                    content = target.read_bytes()
                except OSError as exc:
                    raise GitCommandError(f"cannot read ignored file {path}: {exc}") from exc
                states[path] = _IgnoredEntry(
                    "file", permissions, hashlib.sha256(content).hexdigest(), content=content
                )
            elif os.path.isdir(target):
                states[path] = _IgnoredEntry("directory", permissions)
            else:
                raise GitCommandError(f"cannot safely inventory special ignored path {path}")
        return states

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
        """Commit exactly the reviewed change set. Never empty, never wider."""
        paths = sorted({path.rstrip("/") for path in paths if path.rstrip("/")})
        if not paths:
            raise EmptySnapshotError(
                f"{self.root} has no change to commit; a run must not record an empty commit"
            )
        self.run_git("add", "-A", "--", *paths)
        existing_paths = [path for path in paths if os.path.lexists(self.root / path)]
        if existing_paths:
            self.run_git("add", "-f", "--", *existing_paths)
        staged = {
            path
            for path in self.run_git("diff", "--cached", "--name-only", "-z", strip=False).split(
                "\0"
            )
            if path
        }
        unexpected = sorted(staged - set(paths))
        if unexpected:
            raise GitCommandError(
                "reviewed snapshot would commit paths outside its inventory: "
                + ", ".join(unexpected)
            )
        if not staged:
            raise EmptySnapshotError(
                f"{self.root} has no staged change to commit; a run must not record an empty commit"
            )
        self.run_git("commit", "-q", "-m", message)
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


def _states_from_inventory(
    inventory: list[str] | Mapping[str, _IgnoredEntry],
) -> dict[str, _IgnoredEntry]:
    states = getattr(inventory, "states", None)
    if states is not None:
        return dict(states)
    if isinstance(inventory, Mapping):
        return dict(inventory)
    return {}


def _state_signature(entry: _IgnoredEntry | None) -> tuple | None:
    return entry.signature if entry is not None else None


def _path_depth(path: str) -> int:
    return path.count("/")


def _ensure_relative_path(root: Path, target: Path) -> None:
    try:
        target.parent.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise GitCommandError(f"ignored path escaped worktree root: {target}") from exc


def _remove_path(target: Path) -> None:
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    elif mode:
        target.unlink()


def _restore_entry(target: Path, entry: _IgnoredEntry) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "file":
        target.write_bytes(entry.content or b"")
        target.chmod(entry.mode)
    elif entry.kind == "symlink":
        target.symlink_to(entry.link_target or "")
    elif entry.kind == "directory":
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(entry.mode)
