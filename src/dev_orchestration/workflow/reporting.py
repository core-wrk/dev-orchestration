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
