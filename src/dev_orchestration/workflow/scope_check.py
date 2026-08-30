"""Enforce the scope fence against every Git change state."""

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence


class ScopeViolation(RuntimeError):
    """The run changed a path outside its scope fence."""


def check_diff_against_fence(repo: GitRepo, base_ref: str, fence: ScopeFence) -> list[str]:
    return fence.violations(repo.change_inventory(base_ref))


def check_ignored_writes(
    repo: GitRepo,
    before: list[str],
    fence: ScopeFence,
) -> list[str]:
    """Return ignored paths whose content or type changed outside the fence."""
    return fence.violations(repo.ignored_delta(before))


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
