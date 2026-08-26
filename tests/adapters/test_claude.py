from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import AgentRequest
from dev_orchestration.adapters.claude import (
    DEFAULT_TIMEOUT_SECONDS,
    READ_ONLY_TOOLS,
    ClaudeAdapter,
)


def test_read_only_tools_contains_only_read_only_tools():
    assert READ_ONLY_TOOLS == ("Read", "Grep", "Glob")
    assert list(READ_ONLY_TOOLS) == ["Read", "Grep", "Glob"]


def test_command_uses_print_mode_and_json_output():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt="review this", cwd=Path("/work"))
    )
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-2] == "-p"
    assert cmd[-1] == "review this"


def test_allowed_tools_are_comma_joined_in_one_argument():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(
            role="plan_reviewer",
            prompt="review",
            cwd=Path("/work"),
            allowed_tools=list(READ_ONLY_TOOLS),
        )
    )
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_model_alias_is_passed_through():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt="r", cwd=Path("/w"), model_alias="opus")
    )
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_prompt_is_a_separate_argv_element_not_interpolated():
    injected = 'review"; rm -rf /'
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt=injected, cwd=Path("/w"))
    )
    assert cmd[-1] == injected


def test_prompt_flag_is_last_even_with_tools():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(
            role="plan_reviewer",
            prompt="review this",
            cwd=Path("/w"),
            allowed_tools=list(READ_ONLY_TOOLS),
        )
    )
    assert cmd[-2] == "-p"
    assert cmd[-1] == "review this"


class _FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _capture_subprocess_run(monkeypatch, *, stdout="", returncode=0):
    """Monkeypatch subprocess.run inside the claude module and capture the call."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProcess(stdout=stdout, returncode=returncode)

    monkeypatch.setattr("dev_orchestration.adapters.claude.subprocess.run", fake_run)
    return calls


def test_healthcheck_reports_unavailable_without_a_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    status = ClaudeAdapter().healthcheck()
    assert status.available is False
    assert "not found" in status.detail.lower()


def test_healthcheck_reports_version_and_capabilities(monkeypatch):
    adapter = ClaudeAdapter(binary="claude")
    _capture_subprocess_run(monkeypatch, stdout="claude 1.0.0\n")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/claude")
    status = adapter.healthcheck()
    assert status.available is True
    assert status.version == "claude 1.0.0"
    assert status.path == "/usr/local/bin/claude"
    assert status.capabilities["exec"] is True
    assert status.capabilities["read_only_review"] is True


def test_run_passes_the_requests_timeout_seconds(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = ClaudeAdapter()
    request = AgentRequest(
        role="plan_reviewer", prompt="p", cwd=Path("/w"), timeout_seconds=42
    )
    adapter.run(request)
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["timeout"] == 42


def test_run_falls_back_to_the_default_timeout_when_unset(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = ClaudeAdapter()
    request = AgentRequest(role="plan_reviewer", prompt="p", cwd=Path("/w"))
    assert request.timeout_seconds is None
    adapter.run(request)
    _, kwargs = calls[0]
    assert kwargs["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_run_passes_cwd_from_the_request(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = ClaudeAdapter()
    request = AgentRequest(role="plan_reviewer", prompt="p", cwd=Path("/some/work/dir"))
    adapter.run(request)
    _, kwargs = calls[0]
    assert kwargs["cwd"] == Path("/some/work/dir")


def test_run_maps_agent_result_fields_correctly(monkeypatch):
    _capture_subprocess_run(monkeypatch, stdout='{"verdict": "PASS"}', returncode=7)
    adapter = ClaudeAdapter()
    request = AgentRequest(
        role="plan_reviewer", prompt="p", cwd=Path("/w"), model_alias="opus"
    )
    before = datetime.now(UTC)
    result = adapter.run(request)
    after = datetime.now(UTC)

    assert result.provider == "claude"
    assert result.model == "opus"
    assert result.exit_code == 7
    assert result.output == {"verdict": "PASS"}
    assert result.started_at.tzinfo is not None
    assert result.completed_at.tzinfo is not None
    assert before <= result.started_at <= result.completed_at <= after


def test_run_falls_back_to_raw_stdout_when_nothing_parses(monkeypatch):
    _capture_subprocess_run(monkeypatch, stdout="not json at all\n")
    adapter = ClaudeAdapter()
    request = AgentRequest(role="plan_reviewer", prompt="p", cwd=Path("/w"))
    result = adapter.run(request)
    assert result.output == "not json at all\n"


def test_supports_exec_and_read_only_review_capabilities():
    adapter = ClaudeAdapter()
    assert adapter.supports("exec") is True
    assert adapter.supports("read_only_review") is True
    assert adapter.supports("unknown") is False
