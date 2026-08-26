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


# Verbs that no caller of GitRepo.run_git may ever pass as the first
# argument. This table (and the check below) is the single, auditable home
# for the runtime enforcement of that prohibition — it lives here, rather
# than in repo.py, so repo.py never has to contain the literals "push" or
# "merge" itself and can be fully covered by the plain-text invocation scan
# in tests/git/test_guards.py.
PROHIBITED_VERBS = {
    "push": push,
    "merge": merge,
}


def reject_prohibited_verb(verb: str) -> None:
    """Raise if `verb` is a prohibited git verb.

    Works even when `verb` is assembled dynamically at call time (e.g.
    `"pu" + "sh"`), which a static source scan cannot see — the comparison
    happens against the actual runtime string value, not source text.
    """
    handler = PROHIBITED_VERBS.get(verb)
    if handler is not None:
        handler()
