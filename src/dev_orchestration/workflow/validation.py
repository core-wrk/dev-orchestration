"""Run configured validation independently of provider narration."""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import Tier
from dev_orchestration.git.repo import GitRepo

TAIL_CHARS = 4000


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
    linked: list[tuple[Path, Path]] = []
    copied: list[Path] = []
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
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source, target_is_directory=True)
                except OSError as exc:
                    if relative not in required_paths:
                        raise
                    raise ValidationEnvironmentError(
                        f"required validation directory {relative!r} could not be linked: {exc}"
                    ) from exc
                else:
                    linked.append((target, source))
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
            copied.append(target)
            shutil.copyfile(source, target)
        yield
    finally:
        for target in copied:
            target.unlink(missing_ok=True)
        for target, source in linked:
            if target.is_symlink() and target.resolve() == source:
                target.unlink()


def validation_file_hashes(repo: GitRepo, paths: list[str]) -> dict[str, str]:
    """Validate configured ignored inputs and return their content hashes."""
    ignored = set(repo.ignored_paths())
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
        if repo.is_tracked(relative):
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} is tracked; use validation_links or a tracked source"
            )
        if relative not in ignored:
            raise ValidationEnvironmentError(
                f"declared validation file {relative!r} must be Git-ignored and untracked"
            )
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
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
            proc = subprocess.run(
                ["/bin/sh", "-c", spec.command],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=spec.timeout_seconds,
                check=False,
            )
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
            outcomes.append(
                ValidationOutcome(
                    name=name,
                    command=spec.command,
                    exit_code=None,
                    passed=False,
                    stdout_tail="",
                    stderr_tail=str(exc),
                )
            )
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


def all_passed(outcomes: list[ValidationOutcome]) -> bool:
    return all(outcome.passed for outcome in outcomes)
