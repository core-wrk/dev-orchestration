"""Enforce the scope fence against every Git change state."""

from pathlib import PurePosixPath

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence


class ScopeViolation(RuntimeError):
    """The run changed a path outside its scope fence."""


# These paths are reproducible caches owned by the local language/tooling
# stack.  The runner snapshots and removes them around every stage, so they
# cannot become run residue.  All other ignored writes remain visible to the
# fence and are still violations; in particular, this is not an allowlist for
# ignored application data.
_EPHEMERAL_IGNORED_DIRECTORIES = frozenset(
    {".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
_EPHEMERAL_IGNORED_FILES = frozenset({".coverage"})


def _is_ephemeral_ignored_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return path in _EPHEMERAL_IGNORED_FILES or any(
        part in _EPHEMERAL_IGNORED_DIRECTORIES for part in parts
    )


def check_diff_against_fence(repo: GitRepo, base_ref: str, fence: ScopeFence) -> list[str]:
    return fence.violations(repo.change_inventory(base_ref))


def check_ignored_writes(
    repo: GitRepo,
    before: list[str],
    fence: ScopeFence,
) -> list[str]:
    """Return ignored paths whose content or type changed outside the fence."""
    deltas = repo.ignored_delta(before)
    return fence.violations(path for path in deltas if not _is_ephemeral_ignored_path(path))


def enforce_ignored_writes(repo: GitRepo, before: list[str], fence: ScopeFence) -> None:
    offenders = check_ignored_writes(repo, before, fence)
    if offenders:
        raise ScopeViolation(
            "run wrote git-ignored files outside the scope fence: "
            + ", ".join(offenders)
            + f". Fence allows {fence.include} and excludes {fence.exclude}. "
            "Ignored files never reach the diff the reviewer sees, so the fence "
            "is the only place this can be caught."
        )


def enforce_fence(repo: GitRepo, base_ref: str, fence: ScopeFence) -> None:
    offenders = check_diff_against_fence(repo, base_ref, fence)
    if offenders:
        raise ScopeViolation(
            "run modified files outside the scope fence: "
            + ", ".join(offenders)
            + f". Fence allows {fence.include} and excludes {fence.exclude}."
        )
