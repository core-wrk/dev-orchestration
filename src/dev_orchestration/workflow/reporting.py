"""Human-readable run status and run listing."""

import json
from pathlib import Path

from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.run import RunManifest

NEXT_ACTION: dict[RunState, str] = {
    RunState.CREATED: "start the run with `dev-orch run`",
    RunState.CLASSIFIED: "the tier has been selected; the next stage will start automatically",
    RunState.PLANNED: "the plan is awaiting review",
    RunState.PLAN_REVIEWED: "the plan review is being reconciled",
    RunState.PLAN_FINALIZED: "execution starts next",
    RunState.AWAITING_APPROVAL: "review the saved plan, then run `dev-orch approve RUN_ID`",
    RunState.APPROVED: "run `dev-orch resume RUN_ID --checkpoint-digest DIGEST` in the linked cloud checkout",
    RunState.EXECUTING: "the implementation worker is running in the isolated worktree",
    RunState.VALIDATING: "configured validation commands are running",
    RunState.IMPLEMENTATION_REVIEW: "the actual diff is being reviewed",
    RunState.REMEDIATION: "blocking findings are being addressed",
    RunState.FINAL_VERIFICATION: "acceptance criteria are being verified",
    RunState.PAUSED_USAGE: "resume after the provider's usage window resets",
    RunState.PAUSED_INTERRUPTED: "run `dev-orch resume RUN_ID` after inspecting the interruption",
    RunState.COMPLETE_LOCAL: "review the local branch; dev-orch never pushes, merges, or deploys",
    RunState.BLOCKED: "inspect the last event and make the required human decision",
    RunState.ESCALATED: "inspect the last event and resolve the safety escalation",
    RunState.FAILED: "inspect the last event and failure evidence",
    RunState.CANCELLED: "start a new run if the request is still needed",
}


def describe_status(
    manifest: RunManifest,
    events: list[dict],
    checkpoint: dict | None = None,
    resume_blocker: str | None = None,
) -> str:
    lines = [
        f"Run:    {manifest.run_id}",
        f"Tier:   {manifest.tier}",
        f"Stage:  {manifest.status}",
    ]
    if manifest.git.branch:
        lines.append(f"Branch: {manifest.git.branch}")
    if checkpoint is not None:
        lines.append(f"Next role: {checkpoint.get('next_stage', 'unknown')}")
    if manifest.pause is not None:
        lines.append(f"Provider: {manifest.pause.provider}")
        lines.append(f"Usage: {manifest.pause.limit_kind}")
        lines.append(
            f"Retry: {manifest.pause.next_attempt_at.isoformat()}"
            if manifest.pause.next_attempt_at
            else "Retry: manual"
        )
        if manifest.auto_resume and manifest.pause.account_id is None:
            lines.append("Account: unverified; current login will be used")
        if manifest.status is RunState.PAUSED_INTERRUPTED:
            lines.append("Manual action: inspect partial work, then resume explicitly")
    elif manifest.auto_resume:
        lines.append("Usage: unknown (no saved quota signal)")
    if manifest.cloud_session is not None:
        lines.append(
            f"Cloud: {manifest.cloud_session.provider} {manifest.cloud_session.session_id}"
        )
        if manifest.cloud_session.provider == "codex":
            lines.append("Codex Cloud auto-resume unavailable")
        elif manifest.cloud_session.provider == "claude":
            lines.append(
                "Claude Cloud auto-resume unavailable pending live session and worktree test"
            )
        if manifest.cloud_session.last_state in {"missing", "archived", "action_required"}:
            lines.append(
                "Manual action: inspect the cloud session and resume from intact artifacts"
            )
    elif manifest.status is RunState.AWAITING_APPROVAL:
        lines.append("Manual action: link the intact cloud session before approval")
    if resume_blocker:
        lines.append(f"Resume: refused: {resume_blocker}")
    if events:
        last = events[-1]
        detail = last.get("detail") or last.get("outcome") or ""
        lines.append(f"Last:   {last.get('event', 'unknown')} {detail}".rstrip())
    lines.append(f"Next:   {NEXT_ACTION[manifest.status]}")
    return "\n".join(lines)


def read_events(store_root: Path) -> list[dict]:
    path = store_root / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def list_runs(repo_root: Path) -> list[tuple[str, str]]:
    runs_dir = repo_root / ".ai" / "runs"
    if not runs_dir.is_dir():
        return []
    listed: list[tuple[str, str]] = []
    for directory in sorted(runs_dir.iterdir(), reverse=True):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        listed.append((manifest.run_id, str(manifest.status)))
    return listed


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def summarize_token_usage(events: list[dict]) -> list[dict]:
    """Aggregate token_usage events by role, provider, model and reasoning effort.

    Calls whose provider reported no usage (timeouts, signals, unparsed output)
    are counted in ``calls`` and ``unreported`` so a gap is visible, not silent.
    """
    rows: dict[tuple, dict] = {}
    for event in events:
        if event.get("event") != "token_usage":
            continue
        key = (
            event.get("role"),
            event.get("provider"),
            event.get("model"),
            event.get("reasoning"),
        )
        row = rows.setdefault(
            key,
            {
                "role": key[0],
                "provider": key[1],
                "model": key[2],
                "reasoning": key[3],
                "calls": 0,
                "unreported": 0,
                "duration_seconds": 0.0,
                "cost_usd": 0.0,
                **dict.fromkeys(TOKEN_FIELDS, 0),
            },
        )
        row["calls"] += 1
        row["duration_seconds"] += event.get("duration_seconds") or 0.0
        usage = event.get("usage")
        if not isinstance(usage, dict):
            row["unreported"] += 1
            continue
        for field in TOKEN_FIELDS:
            row[field] += usage.get(field) or 0
        row["cost_usd"] += usage.get("cost_usd") or 0.0
    return sorted(rows.values(), key=lambda row: row["total_tokens"], reverse=True)


def render_token_usage(rows: list[dict]) -> str:
    """Render the summary as a plain-text table, biggest cost sink first."""
    if not rows:
        return "No token usage recorded."
    header = ("role", "model", "effort", "calls", "input", "cached", "output", "total", "cost$")
    body = [
        (
            str(row["role"]),
            f"{row['provider']}/{row['model'] or '?'}",
            str(row["reasoning"] or "-"),
            f"{row['calls']}" + (f" ({row['unreported']} unreported)" if row["unreported"] else ""),
            f"{row['input_tokens'] + row['cache_creation_tokens']:,}",
            f"{row['cached_input_tokens']:,}",
            f"{row['output_tokens']:,}",
            f"{row['total_tokens']:,}",
            f"{row['cost_usd']:.4f}" if row["cost_usd"] else "-",
        )
        for row in rows
    ]
    totals = (
        "TOTAL",
        "",
        "",
        str(sum(row["calls"] for row in rows)),
        f"{sum(row['input_tokens'] + row['cache_creation_tokens'] for row in rows):,}",
        f"{sum(row['cached_input_tokens'] for row in rows):,}",
        f"{sum(row['output_tokens'] for row in rows):,}",
        f"{sum(row['total_tokens'] for row in rows):,}",
        f"{sum(row['cost_usd'] for row in rows):.4f}" if any(r["cost_usd"] for r in rows) else "-",
    )
    table = [header, *body, totals]
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    return "\n".join(
        "  ".join(
            cell.ljust(width) if i < 3 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(line, widths, strict=True))
        ).rstrip()
        for line in table
    )
