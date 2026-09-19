"""Run configured validation independently of provider narration."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import Tier

TAIL_CHARS = 4000


@contextmanager
def validation_environment(
    base_root: Path | None,
    worktree_root: Path,
    link_paths: tuple[str, ...] = (".venv",),
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
    if base_root is None:
        yield
        return
    linked: list[tuple[Path, Path]] = []
    for relative in link_paths:
        source = (base_root / relative).resolve()
        target = worktree_root / relative
        if source.is_dir() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=True)
            linked.append((target, source))
    try:
        yield
    finally:
        for target, source in linked:
            if target.is_symlink() and target.resolve() == source:
                target.unlink()


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
