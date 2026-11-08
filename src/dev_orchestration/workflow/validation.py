"""Run configured validation independently of provider narration."""

import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from time import sleep

from pydantic import BaseModel

from dev_orchestration.adapters.usage import RETRY_BACKOFF, TRANSIENT_LAUNCH_ERRNOS
from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import Tier
from dev_orchestration.git.repo import GitCommandError, GitRepo

TAIL_CHARS = 4000


def _unlink_worktree_entry(
    root_fd: int, relative: str, expected_symlink: Path | None = None
) -> None:
    """Remove an entry through the original worktree directory, never through symlink parents."""
    parts = PurePosixPath(relative).parts
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if expected_symlink is not None:
            if not stat.S_ISLNK(info.st_mode):
                return
            if Path(os.readlink(name, dir_fd=parent_fd)) != expected_symlink:
                return
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


class ValidationEnvironmentError(RuntimeError):
    """A required validation dependency is unavailable in the worktree."""


@contextmanager
def validation_environment(
    base_root: Path | None,
    worktree_root: Path,
    link_paths: tuple[str, ...] = (".venv",),
    required_paths: tuple[str, ...] = (),
    validation_files: tuple[str, ...] = (),
) -> Iterator[None]:
    """Expose the base checkout's ignored dependency directories temporarily.

    Git worktrees do not copy ignored directories such as ``.venv`` or a
    Node project's ``node_modules``. A repository-relative validation
    command must still execute against the worktree's source, so provide
    only the missing directories for the validation subprocess and remove
    each link immediately afterward. ``link_paths`` defaults to the
    Python-only ``.venv`` for backward compatibility; a project declares
    its own set (e.g. ``node_modules``, ``functions/node_modules``) via
    ``ProjectConfig.validation_links``.
    """
    linked: list[tuple[str, Path]] = []
    copied: list[str] = []
    try:
        root_fd = os.open(worktree_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValidationEnvironmentError(
            f"worktree root is unavailable for validation cleanup: {exc}"
        ) from exc
    try:
        try:
            for relative in link_paths:
                target = worktree_root / relative
                source = (base_root / relative).resolve() if base_root is not None else None
                if (
                    source is not None
                    and source.is_dir()
                    and not target.exists()
                    and not target.is_symlink()
                ):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source, target_is_directory=True)
                    linked.append((relative, source))
            for relative in required_paths:
                target = worktree_root / relative
                if not target.is_dir() or not os.access(target, os.R_OK | os.X_OK):
                    raise ValidationEnvironmentError(
                        f"required validation directory {relative!r} is missing or inaccessible"
                    )
            if validation_files and base_root is None:
                raise ValidationEnvironmentError("validation files require a base repository")
            for relative in validation_files:
                source = base_root / relative
                target = worktree_root / relative
                if source.is_symlink() or not source.is_file():
                    raise ValidationEnvironmentError(
                        f"declared validation file {relative!r} is missing or is not an ordinary file"
                    )
                if target.exists() or target.is_symlink():
                    raise ValidationEnvironmentError(
                        f"declared validation file target {relative!r} already exists in the worktree"
                    )
                parent = worktree_root
                for part in PurePosixPath(relative).parts[:-1]:
                    parent = parent / part
                    if parent.is_symlink():
                        raise ValidationEnvironmentError(
                            f"declared validation file target {relative!r} crosses a worktree symlink"
                        )
                target.parent.mkdir(parents=True, exist_ok=True)
                copied.append(relative)
                shutil.copyfile(source, target)
        except OSError as exc:
            raise ValidationEnvironmentError(
                f"validation environment could not be prepared: {exc}"
            ) from exc
        yield
    finally:
        cleanup_errors: list[str] = []
        try:
            current_root = os.stat(worktree_root, follow_symlinks=False)
            original_root = os.fstat(root_fd)
            if (current_root.st_dev, current_root.st_ino) != (
                original_root.st_dev,
                original_root.st_ino,
            ):
                cleanup_errors.append("worktree root was replaced during validation")
        except OSError as exc:
            cleanup_errors.append(str(exc))
        try:
            for relative in copied:
                try:
                    _unlink_worktree_entry(root_fd, relative)
                except OSError as exc:
                    cleanup_errors.append(f"{relative}: {exc}")
            for relative, source in linked:
                try:
                    _unlink_worktree_entry(root_fd, relative, expected_symlink=source)
                except OSError as exc:
                    cleanup_errors.append(f"{relative}: {exc}")
            if cleanup_errors:
                raise ValidationEnvironmentError(
                    "validation environment cleanup could not safely remove a worktree entry: "
                    + "; ".join(cleanup_errors)
                )
        finally:
            os.close(root_fd)


def validation_file_hashes(repo: GitRepo, paths: list[str]) -> dict[str, str]:
    """Validate configured ignored inputs and return their content hashes."""
    if not paths:
        return {}
    hashes: dict[str, str] = {}
    root = repo.root.resolve()
    for relative in paths:
        path = PurePosixPath(relative)
        source = repo.root.joinpath(*path.parts)
        current = repo.root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValidationEnvironmentError(
                    f"declared validation file {relative!r} may not contain symlinks"
                )
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} is unavailable: {exc}"
            ) from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} is unavailable or outside the repository"
            )
        try:
            tracked = repo.is_tracked(relative)
        except (GitCommandError, OSError, subprocess.SubprocessError) as exc:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} could not be checked for tracking: {exc}"
            ) from exc
        if tracked:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} is tracked; use validation_links or a tracked source"
            )
        try:
            ignored = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo.root),
                    "check-ignore",
                    "--quiet",
                    "--no-index",
                    "--",
                    relative,
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} could not be checked against Git ignore rules: {exc}"
            ) from exc
        if ignored.returncode not in {0, 1}:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} could not be checked against Git ignore rules"
            )
        if ignored.returncode != 0:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} must be Git-ignored and untracked"
            )
        try:
            hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} could not be read: {exc}"
            ) from exc
    return hashes


class ValidationOutcome(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    name: str
    command: str
    exit_code: int | None
    passed: bool
    stdout_tail: str
    stderr_tail: str


def run_validations(
    commands: dict[str, ValidationCommand], tier: Tier, cwd: Path
) -> list[ValidationOutcome]:
    outcomes: list[ValidationOutcome] = []
    for name, spec in commands.items():
        if tier not in spec.required_for:
            continue
        try:
            proc = _run_check(spec, cwd)
        except subprocess.TimeoutExpired:
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=None,
                    passed=False,
                    stdout_tail="",
                    stderr_tail=f"timed out after {spec.timeout_seconds}s",
                )
            )
        except OSError as exc:
            raise ValidationEnvironmentError(
                f"validation command {name!r} could not be started: {exc}"
            ) from exc
        else:
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=proc.returncode,
                    passed=proc.returncode == 0,
                    stdout_tail=proc.stdout[-TAIL_CHARS:],
                    stderr_tail=proc.stderr[-TAIL_CHARS:],
                )
            )
    return outcomes


def _run_check(spec: ValidationCommand, cwd: Path) -> subprocess.CompletedProcess:
    for delay in (*RETRY_BACKOFF, None):
        try:
            return subprocess.run(
                ["/bin/sh", "-c", spec.command],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=spec.timeout_seconds,
                check=False,
            )
        except OSError as exc:
            if delay is None or exc.errno not in TRANSIENT_LAUNCH_ERRNOS:
                raise
            sleep(delay)
    raise AssertionError("final validation attempt must return or raise")


def all_passed(outcomes: list[ValidationOutcome]) -> bool:
    return all(outcome.passed for outcome in outcomes)
