import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import (
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    FakeAdapter,
)
from dev_orchestration.adapters.usage import UsageAwareAdapter
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.domain.enums import ProjectClass, Tier
from dev_orchestration.domain.run import RunManifest


class _Metered(FakeAdapter):
    name = "codex"

    def __init__(self, usage=None, exit_code=0, raises=None):
        super().__init__()
        self._usage, self._exit, self._raises = usage, exit_code, raises

    def run(self, request):
        if self._raises:
            raise self._raises
        now = datetime.now(UTC)
        return AgentResult(
            provider="codex",
            model="gpt-5.6-sol",
            exit_code=self._exit,
            output={},
            started_at=now,
            completed_at=now,
            usage=self._usage,
        )


def _store(tmp_path):
    store = RunStore(tmp_path, "run-1")
    store.initialize(
        RunManifest(
            run_id="run-1",
            repository="demo",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    return store


def _events(store):
    lines = (store.root / "events.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == "token_usage"]


def _request(role="plan_reviewer"):
    return AgentRequest(role=role, prompt="p", cwd=Path("/w"), model_alias="sol", reasoning="high")


def test_each_call_appends_a_token_usage_event_with_attempt_numbers(tmp_path):
    store = _store(tmp_path)
    usage = {"input_tokens": 10, "total_tokens": 10}
    adapter = UsageAwareAdapter(_Metered(usage), auto_resume=False, store=store)

    adapter.run(_request())
    adapter.run(_request())

    first, second = _events(store)
    assert (first["role"], first["provider"], first["model"]) == (
        "plan_reviewer",
        "codex",
        "gpt-5.6-sol",
    )
    assert first["reasoning"] == "high"
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["outcome"] == "ok"
    assert first["usage"] == usage


def test_failed_call_is_recorded_with_its_outcome(tmp_path):
    store = _store(tmp_path)
    UsageAwareAdapter(_Metered(None, exit_code=2), auto_resume=False, store=store).run(_request())
    (event,) = _events(store)
    assert event["outcome"] == "failed"
    assert event["usage"] is None


def test_timeout_is_recorded_and_still_raised(tmp_path):
    store = _store(tmp_path)
    adapter = UsageAwareAdapter(
        _Metered(raises=AgentTimeoutError("plan_reviewer", 5, "codex")),
        auto_resume=False,
        store=store,
    )
    with pytest.raises(AgentTimeoutError):
        adapter.run(_request())
    (event,) = _events(store)
    assert event["outcome"] == "AgentTimeoutError"
    assert event["usage"] is None


def test_no_store_records_nothing_and_does_not_fail():
    result = UsageAwareAdapter(_Metered({"total_tokens": 1}), auto_resume=False).run(_request())
    assert result.usage == {"total_tokens": 1}
