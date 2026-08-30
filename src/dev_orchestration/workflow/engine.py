"""The auditable run state machine."""

from datetime import UTC, datetime

from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.run import RunManifest


class IllegalTransitionError(RuntimeError):
    """An attempted state transition is not legal."""


TERMINAL_STATES = frozenset(
    {RunState.COMPLETE_LOCAL, RunState.ESCALATED, RunState.FAILED, RunState.CANCELLED}
)
_ALWAYS = frozenset({RunState.ESCALATED, RunState.FAILED, RunState.CANCELLED})
LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: _ALWAYS | {RunState.CLASSIFIED},
    RunState.CLASSIFIED: _ALWAYS | {RunState.PLANNED, RunState.EXECUTING},
    RunState.PLANNED: _ALWAYS | {RunState.PLAN_REVIEWED},
    RunState.PLAN_REVIEWED: _ALWAYS | {RunState.PLAN_FINALIZED, RunState.PLANNED, RunState.BLOCKED},
    RunState.PLAN_FINALIZED: _ALWAYS
    | {RunState.AWAITING_APPROVAL, RunState.EXECUTING, RunState.BLOCKED},
    RunState.AWAITING_APPROVAL: _ALWAYS | {RunState.APPROVED, RunState.BLOCKED},
    RunState.APPROVED: _ALWAYS | {RunState.EXECUTING, RunState.BLOCKED},
    RunState.EXECUTING: _ALWAYS | {RunState.VALIDATING, RunState.BLOCKED},
    RunState.VALIDATING: _ALWAYS | {RunState.IMPLEMENTATION_REVIEW, RunState.BLOCKED},
    RunState.IMPLEMENTATION_REVIEW: _ALWAYS
    | {RunState.REMEDIATION, RunState.FINAL_VERIFICATION, RunState.BLOCKED},
    RunState.REMEDIATION: _ALWAYS | {RunState.VALIDATING},
    RunState.FINAL_VERIFICATION: _ALWAYS | {RunState.COMPLETE_LOCAL},
    RunState.BLOCKED: _ALWAYS | {RunState.REMEDIATION},
    RunState.COMPLETE_LOCAL: frozenset(),
    RunState.ESCALATED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class Engine:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    @property
    def state(self) -> RunState:
        return self.store.read_manifest().status

    def transition(self, to: RunState) -> RunManifest:
        current = self.state
        allowed = LEGAL_TRANSITIONS.get(current, frozenset())
        if to not in allowed:
            raise IllegalTransitionError(
                f"{current} -> {to} is not a legal transition; legal targets are {sorted(allowed)}"
            )
        manifest = self.store.update_manifest(
            status=to,
            completed_at=datetime.now(UTC) if to in TERMINAL_STATES else None,
        )
        self.store.append_event({"event": "state_changed", "from": str(current), "to": str(to)})
        return manifest
