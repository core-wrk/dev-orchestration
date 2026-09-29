"""Codex adapter.

The binary is frequently not on PATH: it ships inside the ChatGPT desktop
application. Goal Mode exists as a stable feature but has no headless entry
point on the builds verified so far, so it is detected and reported, never
assumed.
"""

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentInterruptedError,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    compose_prompt,
)
from dev_orchestration.adapters.tokens import codex_usage
from dev_orchestration.adapters.usage import UsageObservation, parse_codex_snapshot

CODEX_BUNDLE_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

MODEL_ALIASES = {"sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"}

DEFAULT_TIMEOUT_SECONDS = 1800


def parse_version(text: str) -> str | None:
    match = re.search(r"codex-cli\s+(\S+)", text)
    return match.group(1) if match else None


def discover_codex(override: Path | None = None) -> Path | None:
    """Resolve the codex binary: override -> PATH -> ChatGPT.app bundle."""
    if override is not None:
        return override if Path(override).exists() else None
    on_path = shutil.which("codex")
    if on_path:
        return Path(on_path)
    return CODEX_BUNDLE_PATH if CODEX_BUNDLE_PATH.exists() else None


class CodexAdapter:
    name = "codex"

    def __init__(self, binary: Path | None = None) -> None:
        self.binary = binary
        self._capabilities: dict[str, bool] | None = None

    def _probe(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.binary), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def healthcheck(self) -> AdapterStatus:
        if self.binary is None:
            return AdapterStatus(
                name=self.name,
                available=False,
                detail=(
                    "codex not found on PATH or in "
                    f"{CODEX_BUNDLE_PATH}. Install the Codex CLI or set "
                    "codex_binary in ~/.dev-orchestration/config.yaml."
                ),
            )
        # Same contract as ClaudeAdapter: healthcheck never raises.
        # _detect_capabilities below already guarded its probes; this one did
        # not, so a codex binary that errored took doctor down with it.
        try:
            version = parse_version(self._probe("--version").stdout)
        except (subprocess.SubprocessError, OSError) as exc:
            return AdapterStatus(
                name=self.name,
                available=False,
                path=str(self.binary),
                detail=f"codex found at {self.binary} but did not run: {exc}",
            )
        return AdapterStatus(
            name=self.name,
            available=True,
            version=version,
            path=str(self.binary),
            detail=f"codex {version}",
            capabilities=self._detect_capabilities(),
        )

    def _detect_capabilities(self) -> dict[str, bool]:
        if self._capabilities is not None:
            return self._capabilities
        goals_feature = False
        goal_headless = False
        read_only_review = False
        try:
            features = self._probe("features", "list").stdout
            goals_feature = bool(re.search(r"^goals\s+\S+\s+true", features, re.MULTILINE))
            exec_help = self._probe("exec", "--help").stdout
            goal_headless = "--goal" in exec_help
            read_only_review = "read-only" in exec_help
        except (subprocess.SubprocessError, OSError):
            pass
        self._capabilities = {
            "exec": True,
            "goals_feature": goals_feature,
            "goal_headless": goal_headless,
            "read_only_review": read_only_review,
        }
        return self._capabilities

    def supports(self, capability: str) -> bool:
        return self._detect_capabilities().get(capability, False)

    def observe_usage(self) -> UsageObservation:
        observed = datetime.now(UTC)
        if self.binary is None:
            return UsageObservation(provider=self.name, observed_at=observed)
        try:
            limits, account = self._read_account_snapshot()
        except (OSError, ValueError, subprocess.SubprocessError, TimeoutError):
            return UsageObservation(provider=self.name, observed_at=observed)
        identifier = None
        if isinstance(account, dict):
            routing = account.get("workspaceRouting") or {}
            raw = routing.get("chatgptAccountId")
            if not raw:
                info = account.get("account") or {}
                raw = info.get("email") if isinstance(info, dict) else None
            if isinstance(raw, str) and raw:
                identifier = hashlib.sha256(raw.encode()).hexdigest()
        return parse_codex_snapshot(limits, observed_at=observed, account_id=identifier)

    def authenticated(self) -> bool:
        """Require a subscription login for automatic usage-window recovery."""
        if self.binary is None:
            return False
        try:
            proc = self._probe("login", "status", timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and "ChatGPT" in proc.stdout

    def _read_account_snapshot(self) -> tuple[dict, dict | None]:
        """Use the documented read-only app-server methods; never read token files."""
        proc = subprocess.Popen(
            [str(self.binary), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None and proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 15

        def send(value: dict) -> None:
            proc.stdin.write(json.dumps(value) + "\n")
            proc.stdin.flush()

        def read(target: int) -> dict:
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                value = json.loads(line)
                if value.get("id") == target:
                    if "error" in value:
                        raise ValueError("app-server account read failed")
                    return value.get("result") or {}
            raise TimeoutError("app-server account read timed out")

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "dev-orch", "title": "dev-orch", "version": "1"},
                        "capabilities": {},
                    },
                }
            )
            read(1)
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            limits = read(2)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "account/read",
                    "params": {"refreshToken": False},
                }
            )
            try:
                account = read(3)
            except (TimeoutError, ValueError):
                account = None
            return limits, account
        finally:
            selector.close()
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def build_exec_command(self, request: AgentRequest) -> list[str]:
        cmd = [
            str(self.binary),
            "exec",
            "--json",
            "--ignore-user-config",
            "-C",
            str(request.cwd),
            "-s",
            "read-only" if request.allowed_tools is not None else "workspace-write",
        ]
        if request.model_alias:
            cmd += ["-m", MODEL_ALIASES.get(request.model_alias, request.model_alias)]
        if request.reasoning:
            cmd += ["-c", f"model_reasoning_effort={request.reasoning}"]
        if request.expected_schema:
            cmd += ["--output-schema", str(request.expected_schema)]
        cmd.append(compose_prompt(request))
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        timeout = request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        proc = subprocess.Popen(
            self.build_exec_command(request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=request.cwd,
            start_new_session=True,
        )
        if request.on_process_started is not None:
            request.on_process_started(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(proc)
            raise AgentTimeoutError(request.role, timeout, self.name) from exc
        finally:
            if request.on_process_finished is not None:
                request.on_process_finished()
        if proc.returncode < 0:
            raise AgentInterruptedError(request.role, self.name, -proc.returncode)
        return AgentResult(
            provider=self.name,
            model=MODEL_ALIASES.get(request.model_alias or "", request.model_alias),
            exit_code=proc.returncode,
            output=_last_json_object(stdout) or _last_agent_message(stdout) or stdout,
            started_at=started,
            completed_at=datetime.now(UTC),
            usage=codex_usage(stdout),
            stderr=stderr or "",
            diagnostic=_error_diagnostic(stdout),
        )


def _error_diagnostic(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"error", "turn.failed"}:
            error = event.get("error")
            return {
                "type": event["type"],
                "error": error.get("message", error) if isinstance(error, dict) else error,
                "code": error.get("codexErrorInfo") if isinstance(error, dict) else None,
                "reset_at": error.get("resetsAt") if isinstance(error, dict) else None,
            }
    return None


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop Codex and every MCP child it spawned after a timeout."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def _last_json_object(stdout: str) -> dict | None:
    """Return the structured payload from a Codex JSONL agent message.

    ``codex exec --json`` ends with a ``turn.completed`` usage event. The
    assistant result is nested in the preceding ``item.completed`` event, so
    selecting the last JSON object directly returns metadata instead of the
    requested payload.
    """
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            message = _agent_message_text(parsed)
            if message is not None:
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    return payload
            if parsed.get("type") in {
                "thread.started",
                "turn.started",
                "turn.completed",
                "turn.failed",
                "item.started",
                "item.completed",
                "error",
            }:
                continue
            return parsed
    return None


def _agent_message_text(event: dict) -> str | None:
    """Extract assistant text from a Codex ``item.completed`` event."""
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) else None


def _last_agent_message(stdout: str) -> str | None:
    """Return the last plain assistant message from Codex JSONL output."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            message = _agent_message_text(parsed)
            if message is not None:
                return message
    return None
