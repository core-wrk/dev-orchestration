"""Human-readable run status and run listing."""

import json
from datetime import datetime
from pathlib import Path

from dev_orchestration.artifacts.goals import GoalError, GoalLedger
from dev_orchestration.artifacts.store import RunStore
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
    restart_info: str | None = None,
    finding_summaries: list[tuple[str, str]] | None = None,
    active_provider_recorded: bool | None = None,
) -> str:
    lines = [
        f"Run:    {manifest.run_id}",
        f"Tier:   {manifest.tier}",
        f"Stage:  {manifest.status}",
    ]
    if manifest.git.branch:
        lines.append(f"Branch: {manifest.git.branch}")
    if manifest.status in {RunState.FAILED, RunState.ESCALATED}:
        lines.append(f"Failure class: {manifest.failure_class or 'product (legacy)'}")
        reason = manifest.failure_reason or manifest.terminal_reason
        if reason:
            lines.append(f"Failure reason: {reason}")
    if manifest.status in {RunState.BLOCKED, RunState.FAILED, RunState.ESCALATED}:
        if finding_summaries is None:
            lines.append("Findings: unavailable (no readable saved review summary)")
        elif finding_summaries:
            lines.extend(
                f"Finding {finding_id}: {summary}" for finding_id, summary in finding_summaries
            )
        else:
            lines.append("Findings: none in the latest saved reviews")
    next_action = NEXT_ACTION[manifest.status]
    if manifest.status is RunState.CREATED:
        process_note = (
            "record exists; liveness was not checked"
            if active_provider_recorded
            else "unknown (no active provider record)"
        )
        lines.append(f"Process liveness: {process_note}")
        next_action = "inspect events and checkpoints; resume this run if work began"
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
    if restart_info:
        lines.append(restart_info)
    if events:
        if any(event.get("event") == "prior_failures_unavailable" for event in events):
            lines.append(
                f"Prior failures: source evidence unavailable; source: {manifest.retry_of}"
            )
        if any(event.get("event") == "prior_failures_excluded" for event in events):
            lines.append(
                f"Prior failures: prompt history excluded by policy; source: {manifest.retry_of}"
            )
        repeated = [
            event for event in events if event.get("event") == "validation_failure_repeated"
        ]
        for event in repeated:
            source_paths = ", ".join(event.get("source_artifacts", [])) or "unknown source evidence"
            child_path = event.get("child_artifact", "unknown child evidence")
            lines.append(
                f"Notice: validation failure repeated for {event.get('name', 'command')}; "
                f"source: {source_paths}; child: {child_path}"
            )
        last = events[-1]
        detail = last.get("detail") or last.get("outcome") or ""
        lines.append(f"Last:   {last.get('event', 'unknown')} {detail}".rstrip())
    lines.append(f"Next:   {next_action}")
    return "\n".join(lines)


def read_finding_summaries(store_root: Path) -> list[tuple[str, str]] | None:
    """Read the latest saved plan and implementation findings for one run."""
    paths = []
    for prefix in ("plan-review", "implementation-review"):
        candidates = []
        for path in (store_root / "review").glob(f"{prefix}-v*.json"):
            version = path.stem.rsplit("-v", 1)[-1]
            if version.isdigit():
                candidates.append((int(version), path))
        if candidates:
            paths.append(max(candidates, key=lambda item: item[0])[1])
    if not paths:
        return None

    summaries = []
    try:
        for path in paths:
            result = json.loads(path.read_text(encoding="utf-8"))
            findings = result.get("findings")
            if not isinstance(findings, list):
                return None
            for finding in findings:
                finding_id, summary = finding.get("id"), finding.get("summary")
                if not isinstance(finding_id, str) or not isinstance(summary, str):
                    return None
                summaries.append((finding_id, summary))
    except (OSError, ValueError, AttributeError):
        return None
    return summaries


def read_events(store_root: Path) -> list[dict]:
    path = store_root / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def list_runs(repo_root: Path) -> list[tuple[str, str]]:
    runs_dir = RunStore.runs_directory(repo_root)
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


def _event_time(event: dict) -> datetime | None:
    value = event.get("ts") or event.get("recorded_at")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _completion_coverage(
    runs: list[tuple[Path, RunManifest]], ledger: GoalLedger, epic: str
) -> str:
    completed = [manifest for _, manifest in runs if manifest.status is RunState.COMPLETE_LOCAL]
    if not completed:
        return "incomplete_epics"
    if any(m.completed_at is None or m.completed_at.tzinfo is None for m in completed):
        return "unknown_coverage_epics"
    first = min(completed, key=lambda manifest: manifest.completed_at)
    cutoff = first.completed_at
    try:
        actions = ledger.intervention_events(epic)
    except GoalError:
        return "unknown_coverage_epics"

    incomplete_coverage = False
    completion_logged = False
    for root, manifest in runs:
        if manifest.created_at is not None and manifest.created_at.tzinfo is not None:
            if manifest.created_at > cutoff:
                continue
        else:
            incomplete_coverage = True
        if manifest.epic_id is None or not (root / "events.jsonl").is_file():
            incomplete_coverage = True
            continue
        try:
            events = read_events(root)
        except (OSError, ValueError):
            incomplete_coverage = True
            continue
        if any(not isinstance(event, dict) for event in events):
            incomplete_coverage = True
            continue
        if not any(
            event.get("event") in {"run_branch_reserved", "legacy_recovered"} for event in events
        ):
            incomplete_coverage = True
        actions.extend(event for event in events if event.get("event") == "human_action")
        if manifest.run_id == first.run_id:
            completion_logged = any(
                event.get("event") == "state_changed" and event.get("to") == "COMPLETE_LOCAL"
                for event in events
            )
    action_times = [_event_time(event) for event in actions]
    if any(stamp is not None and stamp <= cutoff for stamp in action_times):
        return "completed_with_human_intervention"
    if incomplete_coverage or not completion_logged or None in action_times:
        return "unknown_coverage_epics"
    return "completed_without_human_intervention"


def summarize_epics(repo_root: Path) -> dict[str, int]:
    """Count first completions, keeping incomplete and unmeasurable history separate."""
    counts = dict.fromkeys(
        (
            "total_linked_epics",
            "completed_without_human_intervention",
            "completed_with_human_intervention",
            "incomplete_epics",
            "unknown_coverage_epics",
            "unlinked_runs",
        ),
        0,
    )
    runs_dir = RunStore.runs_directory(repo_root)
    ledger = GoalLedger(repo_root)
    epics: dict[str, list[tuple[Path, RunManifest]]] = {}
    for path in sorted(runs_dir.glob("*/manifest.json")):
        manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
        epic = manifest.epic_id or manifest.goal_id
        if epic is None:
            counts["unlinked_runs"] += 1
        else:
            epics.setdefault(epic, []).append((path.parent, manifest))
    counts["total_linked_epics"] = len(epics)
    for epic, runs in epics.items():
        counts[_completion_coverage(runs, ledger, epic)] += 1
    return counts


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
