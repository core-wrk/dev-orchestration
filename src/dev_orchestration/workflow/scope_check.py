"""Enforce the scope fence against every Git change state."""

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence


class ScopeViolation(RuntimeError):
    """The run changed a path outside its scope fence."""


def check_diff_against_fence(repo: GitRepo, base_ref: str, fence: ScopeFence) -> list[str]:
    return fence.violations(repo.change_inventory(base_ref))


def enforce_fence(repo: GitRepo, base_ref: str, fence: ScopeFence) -> None:
    offenders = check_diff_against_fence(repo, base_ref, fence)
    if offenders:
        raise ScopeViolation(
            "run modified files outside the scope fence: "
            + ", ".join(offenders)
            + f". Fence allows {fence.include} and excludes {fence.exclude}."
        )
