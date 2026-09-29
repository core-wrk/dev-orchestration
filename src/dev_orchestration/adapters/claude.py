"""Claude Code adapter, driven in non-interactive print mode."""

import json
import os
import shutil
import signal
import subprocess
from datetime import UTC, datetime

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentInterruptedError,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    compose_prompt,
)
from dev_orchestration.adapters.usage import UsageObservation

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

DEFAULT_TIMEOUT_SECONDS = 1800


class ClaudeAdapter:
    name = "claude"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def healthcheck(self) -> AdapterStatus:
        path = shutil.which(self.binary)
        if path is None:
            return AdapterStatus(
                name=self.name,
                available=False,
                detail="claude not found on PATH. Install Claude Code.",
            )
        # healthcheck never raises: a binary that is on PATH but errors, hangs
        # or is not executable is *unavailable*, which is a fact doctor should
        # report, not a traceback that takes the whole command down.
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return AdapterStatus(
                name=self.name,
                available=False,
                path=path,
                detail=f"claude found at {path} but did not run: {exc}",
            )
        version = proc.stdout.strip() or None
        return AdapterStatus(
            name=self.name,
            available=True,
            version=version,
            path=path,
            detail=f"claude {version}",
            capabilities={"exec": True, "read_only_review": True},
        )

    def supports(self, capability: str) -> bool:
        return capability in {"exec", "read_only_review"}

    def observe_usage(self) -> UsageObservation:
        # The installed CLI does not expose a reliable read-only allowance probe.
        return UsageObservation(provider=self.name)

    def authenticated(self) -> bool:
        """Check login without reading credentials or starting a model turn."""
        try:
            proc = subprocess.run(
                [self.binary, "auth", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            status = json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and status.get("loggedIn") is True

    def build_command(self, request: AgentRequest) -> list[str]:
        cmd = [self.binary, "--output-format", "json"]
        if request.model_alias:
            cmd += ["--model", request.model_alias]
        if request.reasoning:
            cmd += ["--effort", request.reasoning]
        if request.allowed_tools:
            tools = ",".join(request.allowed_tools)
            # --tools is the actual restriction: it removes every other
            # built-in tool from the session. --allowedTools alone only
            # pre-approves the listed tools; anything else still exists and
            # merely needs approval. Only with the tool set fenced this way is
            # it safe to skip permission checks, which headless `-p` mode needs
            # because nothing can answer a permission or workspace-trust
            # prompt in a fresh, dev-orch-managed worktree.
            cmd += ["--tools", tools, "--allowedTools", tools, "--dangerously-skip-permissions"]
        if request.expected_schema:
            cmd += ["--json-schema", request.expected_schema.read_text(encoding="utf-8")]
        cmd += ["-p", compose_prompt(request)]
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        timeout = request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        if request.on_process_started is None:
            try:
                proc = subprocess.run(
                    self.build_command(request),
                    capture_output=True,
                    text=True,
                    cwd=request.cwd,
                    timeout=timeout,
                    check=False,
                )
                stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as exc:
                raise AgentTimeoutError(request.role, timeout, self.name) from exc
        else:
            proc = subprocess.Popen(
                self.build_command(request),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=request.cwd,
                start_new_session=True,
            )
            request.on_process_started(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                    except ProcessLookupError:
                        pass
                raise AgentTimeoutError(request.role, timeout, self.name) from exc
            finally:
                if request.on_process_finished is not None:
                    request.on_process_finished()
            returncode = proc.returncode
        if returncode < 0:
            raise AgentInterruptedError(request.role, self.name, -returncode)
        output = _decode_output(stdout)
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            envelope = None
        diagnostic = None
        if isinstance(envelope, dict) and envelope.get("is_error"):
            diagnostic = {
                "type": "error",
                "is_error": True,
                "error": envelope.get("result") or envelope.get("error"),
                "reset_at": envelope.get("reset_at"),
            }
        return AgentResult(
            provider=self.name,
            model=request.model_alias,
            exit_code=returncode,
            output=output,
            started_at=started,
            completed_at=datetime.now(UTC),
            stderr=stderr or "",
            diagnostic=diagnostic,
        )


def _decode_output(stdout: str) -> dict | str:
    """Normalize Claude's JSON result envelope to the provider payload."""
    try:
        output: dict | str = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if not isinstance(output, dict) or "result" not in output:
        return output
    result = output["result"]
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return output
    try:
        decoded = json.loads(result)
    except json.JSONDecodeError:
        return result
    return decoded if isinstance(decoded, dict) else result
