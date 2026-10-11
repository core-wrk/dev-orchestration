"""Small wrapper that gives direct workflow tests explicit isolated goals."""

from itertools import count

from dev_orchestration.workflow.runner import execute_run as _execute_run

_GOALS = count(1)


def execute_run(*args, **kwargs):
    if "goal_id" not in kwargs:
        source = kwargs.get("retry_source")
        kwargs["goal_id"] = (
            source.read_manifest().goal_id if source is not None else f"test-goal-{next(_GOALS)}"
        )
    return _execute_run(*args, **kwargs)
