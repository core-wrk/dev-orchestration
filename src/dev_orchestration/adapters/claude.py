"""Claude Code adapter, driven in non-interactive print mode."""

import json
import shutil
import subprocess
from datetime import UTC, datetime

from dev_orchestration.adapters.base import AdapterStatus, AgentRequest, AgentResult

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
        proc = subprocess.run(
            [self.binary, "--version"], capture_output=True, text=True, timeout=30, check=False
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
        cmd += ["-p", request.prompt]
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        proc = subprocess.run(
            self.build_command(request),
            capture_output=True,
            text=True,
            cwd=request.cwd,
            timeout=request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
        try:
            output: dict | str = json.loads(proc.stdout)
        except json.JSONDecodeError:
            output = proc.stdout
        return AgentResult(
            provider=self.name,
            model=request.model_alias,
            exit_code=proc.returncode,
            output=output,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
