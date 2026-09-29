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
_PAUSE = frozenset({RunState.PAUSED_USAGE, RunState.PAUSED_INTERRUPTED})
LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: _ALWAYS | _PAUSE | {RunState.CLASSIFIED},
    RunState.CLASSIFIED: _ALWAYS | _PAUSE | {RunState.PLANNED, RunState.EXECUTING},
    RunState.PLANNED: _ALWAYS | _PAUSE | {RunState.PLAN_REVIEWED},
    RunState.PLAN_REVIEWED: _ALWAYS
    | _PAUSE
    | {RunState.PLAN_FINALIZED, RunState.PLANNED, RunState.BLOCKED},
    RunState.PLAN_FINALIZED: _ALWAYS
    | _PAUSE
    | {RunState.AWAITING_APPROVAL, RunState.EXECUTING, RunState.BLOCKED},
    RunState.AWAITING_APPROVAL: _ALWAYS | {RunState.APPROVED, RunState.BLOCKED},
    RunState.APPROVED: _ALWAYS | _PAUSE | {RunState.EXECUTING, RunState.BLOCKED},
    RunState.EXECUTING: _ALWAYS | _PAUSE | {RunState.VALIDATING, RunState.BLOCKED},
    # FINAL_VERIFICATION is reachable directly because the trivial tier's stage path
    # omits implementation review; independent verification still runs.
    RunState.VALIDATING: _ALWAYS
    | _PAUSE
    | {RunState.IMPLEMENTATION_REVIEW, RunState.FINAL_VERIFICATION, RunState.BLOCKED},
    RunState.IMPLEMENTATION_REVIEW: _ALWAYS
    | _PAUSE
    | {RunState.REMEDIATION, RunState.FINAL_VERIFICATION, RunState.BLOCKED},
    RunState.REMEDIATION: _ALWAYS | _PAUSE | {RunState.VALIDATING},
    RunState.FINAL_VERIFICATION: _ALWAYS | _PAUSE | {RunState.COMPLETE_LOCAL},
    RunState.PAUSED_USAGE: _ALWAYS
    | {
        RunState.CLASSIFIED,
        RunState.PLANNED,
        RunState.PLAN_REVIEWED,
        RunState.PLAN_FINALIZED,
        RunState.EXECUTING,
        RunState.VALIDATING,
        RunState.IMPLEMENTATION_REVIEW,
        RunState.REMEDIATION,
        RunState.FINAL_VERIFICATION,
    },
    RunState.PAUSED_INTERRUPTED: _ALWAYS
    | {
        RunState.CLASSIFIED,
        RunState.PLANNED,
        RunState.PLAN_REVIEWED,
        RunState.PLAN_FINALIZED,
        RunState.EXECUTING,
        RunState.VALIDATING,
        RunState.IMPLEMENTATION_REVIEW,
        RunState.REMEDIATION,
        RunState.FINAL_VERIFICATION,
    },
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

    def cancel(self, reason: str = "operator requested cancellation") -> RunManifest:
        """Move a non-terminal run to CANCELLED with an auditable reason."""
        if self.state in TERMINAL_STATES:
            raise IllegalTransitionError(f"{self.state} is already terminal")
        self.store.update_manifest(terminal_reason=reason)
        self.store.append_event({"event": "run_cancel_requested", "detail": reason})
        return self.transition(RunState.CANCELLED)
