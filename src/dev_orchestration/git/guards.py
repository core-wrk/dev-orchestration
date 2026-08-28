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


# Verbs that no caller of GitRepo.run_git may ever pass. This table (and the
# check below) is the single, auditable home for the runtime enforcement of
# that prohibition — it lives here, rather than in repo.py, so repo.py never
# has to contain the literals "push" or "merge" itself and can be fully
# covered by the plain-text invocation scan in tests/git/test_guards.py.
#
# "pull" is here because it is a fetch *plus a merge*: it performs a
# prohibited operation under a name that does not contain one.
PROHIBITED_VERBS = {
    "push": push,
    "merge": merge,
    "pull": merge,
}

# Every git verb the framework actually invokes, derived from the real call
# sites in repo.py and worktree.py. The deny-list above states intent; this
# allow-list is what actually holds, because a deny-list only stops the
# operations someone thought to name.
#
# It also closes a bypass that the deny-list alone could not: git accepts
# global options before the subcommand, so `run_git("-c", "k=v", "push", ...)`
# presents "-c" as the first argument and walks straight past a check that
# looks only at prohibited names. An allow-list rejects "-c" outright.
#
# Widening this set is a deliberate act. Add a verb only alongside the call
# site that needs it, and only after checking it cannot reach a remote or
# discard uncommitted work.
ALLOWED_VERBS = frozenset({"add", "checkout", "commit", "diff", "rev-parse", "status", "worktree"})

# `checkout` is allowed because the framework creates branches with it. The
# same verb also discards uncommitted changes when given a pathspec or a
# force flag, which violates the standing rule that dev-orchestration never
# modifies a user's uncommitted work.
_DESTRUCTIVE_CHECKOUT_FLAGS = frozenset({"-f", "--force", "--"})


def reject_prohibited_verb(verb: str) -> None:
    """Raise if `verb` is a prohibited git verb.

    Works even when `verb` is assembled dynamically at call time (e.g.
    `"pu" + "sh"`), which a static source scan cannot see — the comparison
    happens against the actual runtime string value, not source text.
    """
    handler = PROHIBITED_VERBS.get(verb)
    if handler is not None:
        handler()


def reject_disallowed_invocation(args: tuple[str, ...]) -> None:
    """Gate a full `run_git` argument list.

    Checks the prohibited names first, so a caller reaching for `push` gets
    the specific explanation rather than a generic refusal, then requires the
    first argument to be a verb the framework actually uses.
    """
    if not args:
        return
    verb = args[0]
    reject_prohibited_verb(verb)
    if verb not in ALLOWED_VERBS:
        raise ProhibitedOperationError(
            f"git {verb!r} is not an operation dev-orchestration performs. "
            f"Permitted verbs: {', '.join(sorted(ALLOWED_VERBS))}. "
            "A leading global option (such as -c) is rejected here too, because "
            "it would otherwise hide the real subcommand from this check."
        )
    if verb == "checkout" and any(a in _DESTRUCTIVE_CHECKOUT_FLAGS for a in args[1:]):
        raise ProhibitedOperationError(
            "git checkout with a pathspec or force flag discards uncommitted "
            "changes. dev-orchestration never modifies uncommitted work; it "
            "uses checkout only to create a branch."
        )
