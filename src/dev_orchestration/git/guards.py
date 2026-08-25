"""Operations dev-orchestration will not perform.

These exist so the prohibition is explicit and testable rather than merely
absent. V1 has no implementation of any of them.
"""


class ProhibitedOperationError(RuntimeError):
    """An operation forbidden in V1 was attempted."""


def push(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never pushes. Review the local branch and push it yourself."
    )


def merge(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never merges. Merge the local branch yourself."
    )


def deploy(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never deploys. Deployment is a manual step in V1."
    )
