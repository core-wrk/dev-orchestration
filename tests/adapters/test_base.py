from pathlib import Path

from dev_orchestration.adapters.base import AdapterStatus, AgentAdapter, AgentRequest, FakeAdapter


def test_fake_adapter_satisfies_the_protocol():
    assert isinstance(FakeAdapter(), AgentAdapter)


def test_fake_adapter_returns_the_queued_response():
    adapter = FakeAdapter(responses=[{"verdict": "PASS"}])
    result = adapter.run(AgentRequest(role="plan_reviewer", prompt="review", cwd=Path(".")))
    assert result.output == {"verdict": "PASS"}
    assert result.exit_code == 0


def test_fake_adapter_records_every_request():
    adapter = FakeAdapter(responses=[{"a": 1}, {"b": 2}])
    adapter.run(AgentRequest(role="planner", prompt="one", cwd=Path(".")))
    adapter.run(AgentRequest(role="verifier", prompt="two", cwd=Path(".")))
    assert [r.role for r in adapter.requests] == ["planner", "verifier"]


def test_fake_adapter_healthcheck_reports_available():
    assert FakeAdapter().healthcheck().available is True


def test_adapter_status_capabilities_independent_instances():
    """Verify each AdapterStatus gets its own capabilities dict (not shared)."""
    status1 = AdapterStatus(name="a", available=True)
    status2 = AdapterStatus(name="b", available=True)
    # These should be different dict objects
    assert status1.capabilities is not status2.capabilities
