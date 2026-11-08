"""Provider-reported subscription usage, kept separate from token accounting."""

import errno
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import sleep

from dev_orchestration.adapters.base import (
    AgentAdapter,
    AgentInterruptedError,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    agent_result_record,
)
from dev_orchestration.adapters.tokens import claude_usage, codex_usage
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.domain.run import StageActor

RETRY_BACKOFF = (1, 2, 4)
TRANSIENT_LAUNCH_ERRNOS = frozenset({errno.EAGAIN, errno.EINTR})


class ProviderLaunchError(OSError):
    """A provider subprocess could not be launched."""


def launch_provider(command: list[str], **kwargs) -> subprocess.Popen:
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        if exc.errno not in TRANSIENT_LAUNCH_ERRNOS:
            raise
        raise ProviderLaunchError(*exc.args) from exc


_TRANSIENT_PROVIDER = re.compile(
    r"overloaded_error|server_overloaded|temporarily unavailable|"
    r"connection (?:reset|refused)|\b(?:502 bad gateway|503 service unavailable|504 gateway timeout)\b",
    re.IGNORECASE,
)
_CONTEXT_LIMIT = re.compile(
    r"context(?:[ _-]?window)?[ _-]?(?:length|limit|exceeded)|"
    r"(?:token|output token)[ _-]?limit|prompt is too long",
    re.IGNORECASE,
)


def _reported_error(result: AgentResult) -> str:
    diagnostic = result.diagnostic or {}
    return f"{diagnostic.get('code', '')} {diagnostic.get('error', '')} {result.stderr}"


def _partial_usage(exc: OSError | RuntimeError, provider: str) -> dict | None:
    output = getattr(exc.__cause__, "output", None)
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    if not isinstance(output, str):
        return None
    if provider == "codex":
        return codex_usage(output)
    if provider == "claude":
        try:
            return claude_usage(json.loads(output))
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class UsageObservation:
    provider: str
    limit_kind: str = "unknown"
    state: str = "unknown"
    used_percent: float | None = None
    reset_at: datetime | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "unavailable"
    diagnostic: str = ""
    account_id: str | None = None
    raw_diagnostic_ref: str | None = None


class UsageLimitError(RuntimeError):
    def __init__(self, observation: UsageObservation, role: str) -> None:
        self.observation = observation
        self.role = role
        super().__init__(
            f"{observation.provider} usage {observation.state} for {role}: {observation.diagnostic}"
        )


def next_attempt(observation: UsageObservation) -> datetime | None:
    if observation.reset_at is not None:
        return observation.reset_at + timedelta(seconds=30)
    if observation.limit_kind == "five_hour" and observation.state == "exhausted":
        return observation.observed_at + timedelta(hours=5)
    return None


def _parse_reset(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo else None
        except ValueError:
            return None
    return None


def parse_codex_snapshot(
    payload: dict, *, observed_at: datetime, account_id: str | None = None
) -> UsageObservation:
    """Interpret only windows explicitly labelled by the app-server duration."""
    buckets = payload.get("rateLimitsByLimitId")
    snapshots = list(buckets.values()) if isinstance(buckets, dict) else []
    if not snapshots and isinstance(payload.get("rateLimits"), dict):
        snapshots = [payload["rateLimits"]]
    windows = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for label in ("primary", "secondary"):
            window = snapshot.get(label)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins")
            kind = "five_hour" if minutes == 300 else "weekly" if minutes == 10080 else "other"
            used = window.get("usedPercent")
            if not isinstance(used, (int, float)):
                continue
            windows.append((kind, float(used), _parse_reset(window.get("resetsAt"))))
    exhausted = [item for item in windows if item[1] >= 100]
    if exhausted:
        # A run needs every exhausted window to reset. If any window lacks a
        # timestamp, one known reset cannot stand in for the unknown one.
        missing_reset = any(item[2] is None for item in exhausted)
        required = max(
            exhausted,
            key=lambda item: item[2] or datetime.max.replace(tzinfo=UTC),
        )
        return UsageObservation(
            provider="codex",
            limit_kind="unknown" if missing_reset and len(exhausted) > 1 else required[0],
            state="exhausted",
            used_percent=required[1],
            reset_at=None if missing_reset else required[2],
            observed_at=observed_at,
            source="codex_app_server",
            diagnostic="confirmed exhausted usage window",
            account_id=account_id,
        )
    five_hour = [item for item in windows if item[0] == "five_hour"]
    if five_hour:
        used = max(five_hour, key=lambda item: item[1])
        return UsageObservation(
            provider="codex",
            limit_kind="five_hour",
            state="near" if used[1] >= 90 else "available",
            used_percent=used[1],
            reset_at=used[2],
            observed_at=observed_at,
            source="codex_app_server",
            diagnostic="five-hour usage window",
            account_id=account_id,
        )
    return UsageObservation(
        provider="codex",
        observed_at=observed_at,
        source="codex_app_server",
        diagnostic="five-hour window absent",
        account_id=account_id,
    )


_FIVE_HOUR = re.compile(r"(?:five.hour|5.hour|5h)", re.IGNORECASE)
_WEEKLY = re.compile(r"week(?:ly)?", re.IGNORECASE)
_LIMIT = re.compile(
    r"usage limit|hit your limit|"
    r"(?:five.hour|5.hour|5h|weekly?) (?:usage )?limit",
    re.IGNORECASE,
)
_NON_SUBSCRIPTION = re.compile(
    r"credits?|billing|payment|authentication|unauthorized|api key|"
    r"context (?:window|length|limit)|token limit|output token",
    re.IGNORECASE,
)


def rejection(result: AgentResult) -> UsageObservation | None:
    """Only provider error channels may establish a subscription rejection."""
    diagnostic = result.diagnostic or {}
    error = diagnostic.get("error")
    text = str(error or result.stderr or "")
    structured_error = bool(
        diagnostic.get("is_error") or diagnostic.get("type") in {"error", "turn.failed"}
    )
    code = diagnostic.get("code")
    if (
        not (result.exit_code != 0 or structured_error)
        or _NON_SUBSCRIPTION.search(text)
        or not (_LIMIT.search(text) or code == "usageLimitExceeded")
    ):
        return None
    kind = (
        "five_hour" if _FIVE_HOUR.search(text) else "weekly" if _WEEKLY.search(text) else "unknown"
    )
    reset = _parse_reset(diagnostic.get("reset_at") or diagnostic.get("resets_at"))
    if reset is None:
        timestamp = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", text)
        if timestamp:
            reset = _parse_reset(timestamp.group())
    return UsageObservation(
        provider=result.provider,
        limit_kind=kind,
        state="exhausted",
        reset_at=reset,
        observed_at=result.completed_at,
        source="provider_error",
        diagnostic=text,
        account_id=diagnostic.get("account_id"),
    )


class UsageAwareAdapter:
    def __init__(
        self, adapter: AgentAdapter, *, auto_resume: bool, store: RunStore | None = None
    ) -> None:
        self.adapter = adapter
        self.auto_resume = auto_resume
        self.store = store
        self.name = getattr(adapter, "name", "unknown")
        self.binary = getattr(adapter, "binary", None)

    def healthcheck(self):
        return self.adapter.healthcheck()

    def supports(self, capability: str) -> bool:
        return self.adapter.supports(capability)

    def observe_usage(self) -> UsageObservation:
        probe = getattr(self.adapter, "observe_usage", None)
        return probe() if callable(probe) else UsageObservation(provider=self.name)

    def model_id(self, alias: str | None) -> str | None:
        resolve = getattr(self.adapter, "model_id", None)
        return resolve(alias) if callable(resolve) else alias

    def _record_call(
        self,
        request: AgentRequest,
        attempt: int | None,
        *,
        result: AgentResult | None = None,
        outcome: str | None = None,
        usage: dict | None = None,
    ) -> None:
        """Append one token_usage event per provider call; retries count too."""
        if self.store is None:
            return
        event: dict = {
            "event": "token_usage",
            "role": request.role,
            "provider": result.provider if result is not None else self.name,
            "model": (result.model if result is not None else None)
            or self.model_id(request.model_alias),
            "reasoning": request.reasoning,
            "attempt": attempt,
        }
        if result is not None:
            event["outcome"] = "ok" if result.exit_code == 0 else "failed"
            event["exit_code"] = result.exit_code
            event["duration_seconds"] = round(
                (result.completed_at - result.started_at).total_seconds(), 3
            )
            event["usage"] = result.usage
        else:
            event["outcome"] = outcome
            event["usage"] = usage
        self.store.append_event(event)

    def run(self, request: AgentRequest) -> AgentResult:
        if self.store is None:
            return self._run(request)
        actor = StageActor(
            role=request.role, provider=self.name, model=self.model_id(request.model_alias)
        )
        with self.store.stage_operation(request.role, actor):
            return self._run(request)

    def _run(self, request: AgentRequest) -> AgentResult:
        actor = StageActor(
            role=request.role, provider=self.name, model=self.model_id(request.model_alias)
        )
        if self.store is not None:
            self.store.update_manifest(stage_actor=actor, stage_error=None)
        observation = None
        if self.auto_resume:
            try:
                observation = self.observe_usage()
            except (OSError, RuntimeError, TimeoutError, ValueError):
                observation = None
            if (
                observation is not None
                and observation.provider == self.name
                and observation.limit_kind == "five_hour"
                and observation.reset_at is not None
                and (
                    observation.state == "near"
                    or observation.state == "exhausted"
                    or observation.used_percent is not None
                    and observation.used_percent >= 90
                )
            ):
                raise UsageLimitError(observation, request.role)
        for delay in (*RETRY_BACKOFF, None):
            attempt = None
            if self.store is not None:
                attempts = self.store.read_manifest().stage_attempts
                attempt = attempts.get(request.role, 0) + 1
                self.store.update_manifest(stage_attempts={**attempts, request.role: attempt})
            try:
                result = self.adapter.run(request)
            except (AgentTimeoutError, AgentInterruptedError, OSError) as exc:
                self._record_call(
                    request,
                    attempt,
                    outcome=type(exc).__name__,
                    usage=_partial_usage(exc, self.name),
                )
                if not (
                    delay is not None
                    and isinstance(exc, ProviderLaunchError)
                    and exc.errno in TRANSIENT_LAUNCH_ERRNOS
                ):
                    raise
            else:
                self._record_call(request, attempt, result=result)
                limited = rejection(result)
                error = _reported_error(result) if result.exit_code != 0 else ""
                context_limit = bool(_CONTEXT_LIMIT.search(error))
                if self.store is not None:
                    self.store.update_manifest(
                        stage_actor=actor.model_copy(
                            update={
                                "provider": result.provider,
                                "model": result.model or actor.model,
                            }
                        ),
                        stage_error="provider_context_limit" if context_limit else None,
                    )
                if not (
                    delay is not None
                    and limited is None
                    and not context_limit
                    and not _NON_SUBSCRIPTION.search(error)
                    and request.allowed_tools
                    and self.supports("read_only_review")
                    and _TRANSIENT_PROVIDER.search(error)
                ):
                    return self._finish(request, result, limited, observation)
            sleep(delay)
        raise AssertionError("final provider attempt must return or raise")

    def _finish(
        self,
        request: AgentRequest,
        result: AgentResult,
        limited: UsageObservation | None,
        observation: UsageObservation | None,
    ) -> AgentResult:
        if limited is not None:
            if observation is not None and limited.account_id is None:
                limited = replace(limited, account_id=observation.account_id)
            if self.store is not None:
                name = (
                    f"execution/usage-rejection-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.json"
                )
                self.store.write_json_artifact(name, agent_result_record(result), immutable=True)
                limited = replace(limited, raw_diagnostic_ref=name)
            raise UsageLimitError(limited, request.role)
        return result
