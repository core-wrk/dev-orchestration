import errno
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import dev_orchestration.adapters.usage as usage_module
from dev_orchestration.adapters.base import (
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    FakeAdapter,
)
from dev_orchestration.adapters.claude import claude_call_usage
from dev_orchestration.adapters.usage import ProviderLaunchError, UsageAwareAdapter
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


@pytest.mark.parametrize(
    "read_only, failures, attempts", [(True, 2, 3), (True, 4, 4), (False, 1, 1)]
)
def test_transient_provider_retry_is_bounded_and_skips_mutating_calls(
    tmp_path, monkeypatch, read_only, failures, attempts
):
    store = _store(tmp_path)
    store.update_manifest(epic_id="synthetic-epic")
    delays = []
    monkeypatch.setattr(usage_module, "sleep", delays.append)

    class Transient(_Metered):
        calls = 0

        def run(self, request):
            self.calls += 1
            result = super().run(request)
            if self.calls <= failures:
                return replace(result, exit_code=1, diagnostic={"code": "server_overloaded"})
            return result

    request = replace(
        _request("plan_reconciler" if read_only else "implementation_worker"),
        allowed_tools=["Read"] if read_only else None,
    )
    result = UsageAwareAdapter(Transient({"total_tokens": 3}), auto_resume=False, store=store).run(
        request
    )
    ledger = _events(store)
    assert [event["attempt"] for event in ledger] == list(range(1, attempts + 1))
    assert [event["usage"] for event in ledger] == [{"total_tokens": 3}] * attempts
    assert delays == [1, 2, 4][: attempts - 1]
    assert (result.exit_code == 0) == (attempts > failures)
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    started = [event for event in events if event["event"] == "stage_started"]
    finished = [event for event in events if event["event"] == "stage_finished"]
    assert len(started) == len(finished) == 1
    assert started[0]["operation_id"] == finished[0]["operation_id"]


def test_transient_prelaunch_error_can_retry_worker(tmp_path, monkeypatch):
    store = _store(tmp_path)
    delays = []
    monkeypatch.setattr(usage_module, "sleep", delays.append)

    class LaunchOnce(_Metered):
        launched = False

        def run(self, request):
            if not self.launched:
                self.launched = True
                raise ProviderLaunchError(errno.EAGAIN, "synthetic transient launch error")
            return super().run(request)

    wrapped = UsageAwareAdapter(LaunchOnce({"total_tokens": 3}), auto_resume=False, store=store)
    assert wrapped.run(_request("implementation_worker")).exit_code == 0
    assert delays == [1]
    assert [e["usage"] for e in _events(store)] == [None, {"total_tokens": 3}]


def test_timeout_preserves_any_reported_partial_usage(tmp_path):
    store = _store(tmp_path)
    partial = b'{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}\n'

    class Timeout(_Metered):
        def run(self, request):
            raise AgentTimeoutError(request.role, 5, "codex") from subprocess.TimeoutExpired(
                "synthetic", 5, output=partial
            )

    with pytest.raises(AgentTimeoutError):
        UsageAwareAdapter(Timeout(), auto_resume=False, store=store).run(_request())
    assert _events(store)[0]["usage"]["total_tokens"] == 9


def test_claude_reports_model_ids_and_per_model_token_counts():
    model, usage = claude_call_usage(
        {
            "usage": {"input_tokens": 11, "output_tokens": 6},
            "modelUsage": {
                "fixture-version": {
                    "inputTokens": 11,
                    "outputTokens": 6,
                    "cacheReadInputTokens": 2,
                }
            },
        },
        "fixture-alias",
    )
    assert model == "fixture-version"
    assert usage["by_model"][model]["cached_input_tokens"] == 2
    assert usage["by_model"][model]["total_tokens"] == 19
