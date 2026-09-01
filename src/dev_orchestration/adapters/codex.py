"""Codex adapter.

The binary is frequently not on PATH: it ships inside the ChatGPT desktop
application. Goal Mode exists as a stable feature but has no headless entry
point on the builds verified so far, so it is detected and reported, never
assumed.
"""

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentRequest,
    AgentResult,
    compose_prompt,
)

CODEX_BUNDLE_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

MODEL_ALIASES = {"sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"}

DEFAULT_TIMEOUT_SECONDS = 3600


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
        try:
            features = self._probe("features", "list").stdout
            goals_feature = bool(re.search(r"^goals\s+\S+\s+true", features, re.MULTILINE))
            exec_help = self._probe("exec", "--help").stdout
            goal_headless = "--goal" in exec_help
        except (subprocess.SubprocessError, OSError):
            pass
        self._capabilities = {
            "exec": True,
            "goals_feature": goals_feature,
            "goal_headless": goal_headless,
        }
        return self._capabilities

    def supports(self, capability: str) -> bool:
        return self._detect_capabilities().get(capability, False)

    def build_exec_command(self, request: AgentRequest) -> list[str]:
        cmd = [
            str(self.binary),
            "exec",
            "--json",
            "-C",
            str(request.cwd),
            "-s",
            "workspace-write",
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
        proc = subprocess.run(
            self.build_exec_command(request),
            capture_output=True,
            text=True,
            cwd=request.cwd,
            timeout=request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
        return AgentResult(
            provider=self.name,
            model=MODEL_ALIASES.get(request.model_alias or "", request.model_alias),
            exit_code=proc.returncode,
            output=_last_json_object(proc.stdout)
            or _last_agent_message(proc.stdout)
            or proc.stdout,
            started_at=started,
            completed_at=datetime.now(UTC),
        )


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
