"""Provider-neutral agent interface.

Workflow code invokes roles through this protocol and never sees provider
command syntax. Adding a provider means adding an adapter, nothing else.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentRequest:
    role: str
    prompt: str
    cwd: Path
    model_alias: str | None = None
    reasoning: str | None = None
    allowed_tools: list[str] | None = None
    expected_schema: Path | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class AgentResult:
    provider: str
    model: str | None
    exit_code: int
    output: dict | str
    started_at: datetime
    completed_at: datetime
    usage: dict | None = None


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    available: bool
    version: str | None = None
    path: str | None = None
    detail: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    def healthcheck(self) -> AdapterStatus: ...
    def run(self, request: AgentRequest) -> AgentResult: ...
    def supports(self, capability: str) -> bool: ...


class FakeAdapter:
    """Test double. Integration tests drive the engine through this."""

    def __init__(self, responses: list[dict | str] | None = None) -> None:
        self._responses = list(responses or [{}])
        self.requests: list[AgentRequest] = []

    def healthcheck(self) -> AdapterStatus:
        return AdapterStatus(name="fake", available=True, version="0", detail="test double")

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        payload = self._responses.pop(0) if self._responses else {}
        now = datetime.now(UTC)
        return AgentResult(
            provider="fake",
            model=request.model_alias,
            exit_code=0,
            output=payload,
            started_at=now,
            completed_at=now,
        )

    def supports(self, capability: str) -> bool:
        return capability in {"exec"}
