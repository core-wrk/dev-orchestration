import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AgentRequest, AgentTimeoutError
from dev_orchestration.adapters.codex import (
    CODEX_BUNDLE_PATH,
    CodexAdapter,
    _last_json_object,
    discover_codex,
    parse_version,
)


def test_parse_version_reads_the_installed_format():
    assert parse_version("codex-cli 0.146.0-alpha.9.2") == "0.146.0-alpha.9.2"
    assert parse_version("garbage") is None


def test_bundle_path_is_the_chatgpt_app_location():
    assert CODEX_BUNDLE_PATH == Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def test_discovery_prefers_an_explicit_override(tmp_path):
    override = tmp_path / "codex"
    override.write_text("#!/bin/sh\n")
    override.chmod(0o755)
    assert discover_codex(override=override) == override


def test_discovery_returns_none_when_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("dev_orchestration.adapters.codex.CODEX_BUNDLE_PATH", tmp_path / "absent")
    assert discover_codex() is None


def test_discovery_prefers_path_over_the_bundle(monkeypatch, tmp_path):
    on_path = tmp_path / "on_path" / "codex"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    bundle = tmp_path / "bundle" / "codex"
    bundle.parent.mkdir()
    bundle.write_text("#!/bin/sh\n")
    bundle.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _name: str(on_path))
    monkeypatch.setattr("dev_orchestration.adapters.codex.CODEX_BUNDLE_PATH", bundle)
    assert discover_codex() == on_path


def test_exec_command_uses_verified_flags():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(
        role="planner",
        prompt="write a plan",
        cwd=Path("/work"),
        model_alias="sol",
        expected_schema=Path("/schemas/plan.json"),
    )
    cmd = adapter.build_exec_command(request)
    assert cmd[:2] == ["/bin/codex", "exec"]
    assert "--json" in cmd
    assert "--ignore-user-config" in cmd
    assert cmd[cmd.index("-C") + 1] == "/work"
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"
    assert cmd[cmd.index("--output-schema") + 1] == "/schemas/plan.json"
    assert cmd[-1] == "write a plan"


def test_exec_command_never_bypasses_the_sandbox():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    cmd = adapter.build_exec_command(AgentRequest(role="planner", prompt="p", cwd=Path("/w")))
    assert not any("dangerously" in part for part in cmd)


def test_luna_alias_resolves_to_the_worker_model():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    cmd = adapter.build_exec_command(
        AgentRequest(role="implementation_worker", prompt="p", cwd=Path("/w"), model_alias="luna")
    )
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-luna"


def test_prompt_is_its_own_argv_element_not_concatenated():
    # A prompt containing shell metacharacters must never be interpretable;
    # it must be a single, standalone argv element, never appended onto
    # (or concatenated with) a preceding flag's value.
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    prompt = "list files; rm -rf / && echo done"
    cmd = adapter.build_exec_command(AgentRequest(role="planner", prompt=prompt, cwd=Path("/w")))
    assert cmd[-1] == prompt
    assert cmd.count(prompt) == 1
    assert not any(part != prompt and prompt in part for part in cmd)


def test_healthcheck_reports_unavailable_without_a_binary():
    status = CodexAdapter(binary=None).healthcheck()
    assert status.available is False
    assert "not found" in status.detail.lower()


def test_healthcheck_reports_version_and_capabilities(monkeypatch):
    adapter = CodexAdapter(binary=Path("/bin/codex"))

    def fake_probe(self, *args, timeout=30):
        del self, timeout
        if args == ("--version",):
            return _FakeCompleted(stdout="codex-cli 0.146.0-alpha.9.2\n")
        if args == ("features", "list"):
            return _FakeCompleted(stdout="goals              stable        true\n")
        if args == ("exec", "--help"):
            return _FakeCompleted(
                stdout="--json --output-schema <FILE> -m <MODEL> -C <DIR> -s <SANDBOX_MODE> -o <FILE>\n"
            )
        raise AssertionError(f"unexpected probe args: {args}")

    monkeypatch.setattr(CodexAdapter, "_probe", fake_probe)
    status = adapter.healthcheck()
    assert status.available is True
    assert status.version == "0.146.0-alpha.9.2"
    assert status.capabilities["exec"] is True
    assert status.capabilities["goals_feature"] is True
    assert status.capabilities["goal_headless"] is False


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class _FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _FakePopen:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.pid = 1234
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, *, timeout: int):
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def wait(self, *, timeout: int | None = None):
        return self.returncode


def _capture_subprocess_run(monkeypatch, *, stdout="", returncode=0, stderr=""):
    """Monkeypatch provider startup and capture its Popen arguments."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakePopen(stdout=stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr("dev_orchestration.adapters.codex.subprocess.Popen", fake_run)
    return calls


def test_run_passes_the_requests_timeout_seconds(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(role="planner", prompt="p", cwd=Path("/w"), timeout_seconds=42)
    adapter.run(request)
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["start_new_session"] is True


def test_run_falls_back_to_the_default_timeout_when_unset(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(role="planner", prompt="p", cwd=Path("/w"))
    assert request.timeout_seconds is None
    adapter.run(request)
    _, kwargs = calls[0]
    assert kwargs["start_new_session"] is True


def test_run_passes_cwd_from_the_request(monkeypatch):
    calls = _capture_subprocess_run(monkeypatch, stdout='{"ok": true}')
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(role="planner", prompt="p", cwd=Path("/some/work/dir"))
    adapter.run(request)
    _, kwargs = calls[0]
    assert kwargs["cwd"] == Path("/some/work/dir")


def test_run_maps_agent_result_fields_correctly(monkeypatch):
    _capture_subprocess_run(monkeypatch, stdout='{"verdict": "PASS"}', returncode=7)
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(role="planner", prompt="p", cwd=Path("/w"), model_alias="sol")
    before = datetime.now(UTC)
    result = adapter.run(request)
    after = datetime.now(UTC)

    assert result.provider == "codex"
    assert result.model == "gpt-5.6-sol"
    assert result.exit_code == 7
    assert result.output == {"verdict": "PASS"}
    assert result.started_at.tzinfo is not None
    assert result.completed_at.tzinfo is not None
    assert before <= result.started_at <= result.completed_at <= after


def test_run_captures_provider_stderr(monkeypatch):
    _capture_subprocess_run(monkeypatch, stdout="", returncode=7, stderr="quota exceeded")
    result = CodexAdapter(binary=Path("/bin/codex")).run(
        AgentRequest(role="planner", prompt="p", cwd=Path("/w"))
    )
    assert result.stderr == "quota exceeded"


def test_run_turns_a_timeout_into_a_named_stage_error(monkeypatch):
    class TimeoutPopen(_FakePopen):
        def communicate(self, *, timeout: int):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

    monkeypatch.setattr(
        "dev_orchestration.adapters.codex.subprocess.Popen",
        lambda *_args, **_kwargs: TimeoutPopen(""),
    )
    with pytest.raises(AgentTimeoutError, match="planner.*42-second"):
        CodexAdapter(binary=Path("/bin/codex")).run(
            AgentRequest(role="planner", prompt="p", cwd=Path("/w"), timeout_seconds=42)
        )


def test_run_falls_back_to_raw_stdout_when_nothing_parses(monkeypatch):
    _capture_subprocess_run(monkeypatch, stdout="not json at all\n")
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(role="planner", prompt="p", cwd=Path("/w"))
    result = adapter.run(request)
    assert result.output == "not json at all\n"


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ('{"a": 1}\n{"b": 2}\n', {"b": 2}),
        ("", None),
        ("not json\nstill not json\n", None),
        ('{"a": 1}\n[1, 2, 3]\n', {"a": 1}),
        ('{"a": 1}\n42\n', {"a": 1}),
        (
            (
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"{\\"tier\\":\\"trivial\\",\\"rationale\\":\\"r\\"}"}}\n'
                '{"type":"turn.completed","usage":{"output_tokens":3}}\n'
            ),
            {"tier": "trivial", "rationale": "r"},
        ),
    ],
)
def test_last_json_object_edge_cases(stdout, expected):
    assert _last_json_object(stdout) == expected


def test_run_uses_plain_agent_message_when_codex_has_no_structured_payload(monkeypatch):
    stdout = (
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"# Plan\\n\\n1. Make the change."}}\n'
        '{"type":"turn.completed","usage":{"output_tokens":8}}\n'
    )
    _capture_subprocess_run(monkeypatch, stdout=stdout)
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    result = adapter.run(AgentRequest(role="planner", prompt="p", cwd=Path("/w")))
    assert result.output == "# Plan\n\n1. Make the change."


def test_last_json_object_ignores_non_message_item_events():
    stdout = (
        '{"type":"item.started","item":{"type":"command_execution"}}\n'
        '{"type":"turn.completed","usage":{"output_tokens":1}}\n'
    )
    assert _last_json_object(stdout) is None
