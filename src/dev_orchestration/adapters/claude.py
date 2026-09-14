"""Claude Code adapter, driven in non-interactive print mode."""

import json
import shutil
import subprocess
from datetime import UTC, datetime

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    compose_prompt,
)

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

    def build_command(self, request: AgentRequest) -> list[str]:
        cmd = [self.binary, "--output-format", "json"]
        if request.model_alias:
            cmd += ["--model", request.model_alias]
        if request.allowed_tools:
            cmd += ["--allowedTools", ",".join(request.allowed_tools)]
        if request.expected_schema:
            cmd += ["--json-schema", request.expected_schema.read_text(encoding="utf-8")]
        cmd += ["-p", compose_prompt(request)]
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        timeout = request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        try:
            proc = subprocess.run(
                self.build_command(request),
                capture_output=True,
                text=True,
                cwd=request.cwd,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeoutError(request.role, timeout, self.name) from exc
        output = _decode_output(proc.stdout)
        return AgentResult(
            provider=self.name,
            model=request.model_alias,
            exit_code=proc.returncode,
            output=output,
            started_at=started,
            completed_at=datetime.now(UTC),
            stderr=getattr(proc, "stderr", "") or "",
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
