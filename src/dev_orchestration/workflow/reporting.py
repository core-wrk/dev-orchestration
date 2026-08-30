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
    RunState.AWAITING_APPROVAL: "a human must approve the plan",
    RunState.APPROVED: "execution starts next",
    RunState.EXECUTING: "the implementation worker is running in the isolated worktree",
    RunState.VALIDATING: "configured validation commands are running",
    RunState.IMPLEMENTATION_REVIEW: "the actual diff is being reviewed",
    RunState.REMEDIATION: "blocking findings are being addressed",
    RunState.FINAL_VERIFICATION: "acceptance criteria are being verified",
    RunState.COMPLETE_LOCAL: "review the local branch; dev-orch never pushes, merges, or deploys",
    RunState.BLOCKED: "inspect the last event and make the required human decision",
    RunState.ESCALATED: "inspect the last event and resolve the safety escalation",
    RunState.FAILED: "inspect the last event and failure evidence",
    RunState.CANCELLED: "start a new run if the request is still needed",
}


def describe_status(manifest: RunManifest, events: list[dict]) -> str:
    lines = [
        f"Run:    {manifest.run_id}",
        f"Tier:   {manifest.tier}",
        f"Stage:  {manifest.status}",
    ]
    if manifest.git.branch:
        lines.append(f"Branch: {manifest.git.branch}")
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
