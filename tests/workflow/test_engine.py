import json

import pytest

from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.engine import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    Engine,
    IllegalTransitionError,
)


@pytest.fixture
def engine(tmp_path):
    store = RunStore(tmp_path, "run-1")
    store.initialize(
        RunManifest(
            run_id="run-1",
            repository="r",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    return Engine(store)


def test_a_new_run_starts_in_created(engine):
    assert engine.state is RunState.CREATED


def test_a_legal_transition_persists_to_manifest_and_events(engine):
    engine.transition(RunState.CLASSIFIED)
    assert engine.store.read_manifest().status is RunState.CLASSIFIED
    event = json.loads((engine.store.root / "events.jsonl").read_text().splitlines()[0])
    assert event["from"] == "CREATED" and event["to"] == "CLASSIFIED"


def test_an_illegal_transition_does_not_persist_or_emit(engine):
    with pytest.raises(IllegalTransitionError):
        engine.transition(RunState.COMPLETE_LOCAL)
    assert engine.state is RunState.CREATED
    assert not (engine.store.root / "events.jsonl").exists()


def test_terminal_states_accept_no_further_transition(engine):
    engine.transition(RunState.ESCALATED)
    assert engine.state in TERMINAL_STATES
    with pytest.raises(IllegalTransitionError):
        engine.transition(RunState.PLANNED)


def test_every_non_terminal_state_can_escalate():
    for state, allowed in LEGAL_TRANSITIONS.items():
        if state not in TERMINAL_STATES:
            assert RunState.ESCALATED in allowed
