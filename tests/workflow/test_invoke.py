import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AdapterStatus, AgentRequest, AgentResult
from dev_orchestration.domain.findings import Classification
from dev_orchestration.workflow.invoke import (
    AgentInvocationError,
    SchemaEscalation,
    invoke_structured,
)

GOOD = {"tier": "standard", "rationale": "two modules", "profiles": []}
BAD = {"tier": "not-a-tier", "rationale": "x", "profiles": []}


class ScriptedAdapter:
    def __init__(self, payloads, exit_codes=None, stderrs=None):
        self.payloads = list(payloads)
        self.exit_codes = list(exit_codes or [0] * len(self.payloads))
        self.stderrs = list(stderrs or [""] * len(self.payloads))
        self.calls = 0
        self.requests = []

    def healthcheck(self):
        return AdapterStatus(name="scripted", available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.calls += 1
        self.requests.append(request)
        now = datetime.now(UTC)
        return AgentResult(
            "scripted",
            None,
            self.exit_codes.pop(0),
            self.payloads.pop(0),
            now,
            now,
            stderr=self.stderrs.pop(0),
        )


def request():
    return AgentRequest(role="classifier", prompt="p", cwd=Path("/w"))


def test_valid_first_response_is_not_retried(tmp_path):
    adapter = ScriptedAdapter([GOOD])
    assert invoke_structured(adapter, request(), Classification, tmp_path).tier == "standard"
    assert adapter.calls == 1
    assert adapter.requests[0].expected_schema is not None


def test_every_structured_call_attaches_generated_schema_to_both_attempts(tmp_path):
    adapter = ScriptedAdapter([BAD, GOOD])
    invoke_structured(adapter, request(), Classification, tmp_path)
    assert adapter.calls == 2
    assert adapter.requests[0].expected_schema == adapter.requests[1].expected_schema
    assert "tier" in adapter.requests[1].prompt


def test_two_invalid_responses_escalate_rather_than_looping(tmp_path):
    adapter = ScriptedAdapter([BAD, BAD])
    with pytest.raises(SchemaEscalation):
        invoke_structured(adapter, request(), Classification, tmp_path)
    assert adapter.calls == 2


def test_provider_nonzero_exit_persists_stderr_and_surfaces_it(tmp_path):
    adapter = ScriptedAdapter([GOOD], exit_codes=[3], stderrs=["service unavailable"])
    with pytest.raises(AgentInvocationError, match="service unavailable"):
        invoke_structured(adapter, request(), Classification, tmp_path)
    failure = json.loads(
        (tmp_path / "execution" / "provider-failure-classifier-attempt-1.json").read_text()
    )
    assert failure["stderr"] == "service unavailable"


def test_the_generated_schema_is_written_where_the_caller_asks(tmp_path):
    adapter = ScriptedAdapter([GOOD])
    invoke_structured(adapter, request(), Classification, tmp_path / "schemas")
    assert (tmp_path / "schemas" / "classification.json").is_file()
    assert adapter.requests[0].expected_schema == tmp_path / "schemas" / "classification.json"
