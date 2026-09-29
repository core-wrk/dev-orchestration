"""Conservative, explicit recovery for runs made before stage receipts existed."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.adapters.usage import rejection
from dev_orchestration.artifacts.store import RunStore, new_run_id
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.domain.enums import Outcome, RunState
from dev_orchestration.domain.findings import ReviewResult
from dev_orchestration.domain.run import GitBlock
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.checkpoints import input_hashes
from dev_orchestration.workflow.scope_check import enforce_fence
from dev_orchestration.workflow.snapshot import capture_snapshot


class LegacyRecoveryError(RuntimeError):
    """The old artifact set does not prove one safe continuation."""


def _confirmed_quota(store: RunStore) -> bool:
    for path in (store.root / "execution").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or "provider" not in value:
                continue
            result = AgentResult(
                provider=value["provider"],
                model=value.get("model"),
                exit_code=value.get("exit_code", 0),
                output=value.get("output", ""),
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                stderr=value.get("stderr", ""),
                diagnostic=value.get("diagnostic"),
            )
            if rejection(result) is not None:
                return True
        except (ValueError, TypeError, KeyError):
            continue
    return False


def _review(path: Path) -> ReviewResult:
    return ReviewResult.model_validate_json(path.read_text(encoding="utf-8"))


def _latest(files: list[Path]) -> Path:
    return max(files, key=lambda path: int(path.stem.rsplit("-v", 1)[1]))


def _infer_next(store: RunStore, config: ProjectConfig) -> tuple[str, int]:
    root = store.root
    manifest = store.read_manifest()
    plans = store.plan_versions()
    plan_reviews = list((root / "review").glob("plan-review-v*.json"))
    impl_reviews = list((root / "review").glob("implementation-review-v*.json"))
    validations = list((root / "execution").glob("validation-v*.json"))
    remediations = list((root / "execution").glob("remediation-v*.json"))
    cycle = len(remediations)
    if (root / "verification/final-verification.json").is_file():
        # Old files did not bind the verified verdict to an exact diff.
        return "verifier", cycle
    if impl_reviews:
        reviewed = _review(_latest(impl_reviews))
        if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
            raise LegacyRecoveryError("old implementation review requires a human decision")
        return ("remediation" if reviewed.blocking_ids() else "verifier"), cycle
    if validations:
        latest = _latest(validations)
        outcomes = json.loads(latest.read_text(encoding="utf-8"))
        if any(not item.get("passed", False) for item in outcomes):
            raise LegacyRecoveryError("old validation failed; no safe next stage")
        from dev_orchestration.workflow.runner import _implementation_review_required

        return (
            "implementation_reviewer"
            if _implementation_review_required(manifest.tier, config.validation)
            else "verifier",
            cycle,
        )
    if (root / "execution/worker-result.json").is_file():
        result = json.loads((root / "execution/worker-result.json").read_text())
        if result.get("exit_code") == 0:
            return "validation", cycle
    if (root / "planning/approved-plan.md").is_file() or (
        root / "planning/task-contract.md"
    ).is_file():
        return "implementation_worker", cycle
    if len(plan_reviews) > len(plans) or len(plans) - len(plan_reviews) > 1:
        raise LegacyRecoveryError("old plan review order is ambiguous")
    if plan_reviews and len(plan_reviews) == len(plans):
        reviewed = _review(_latest(plan_reviews))
        if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
            raise LegacyRecoveryError("old plan review requires a human decision")
        return (
            "plan_reconciler" if reviewed.blocking_ids() else "plan_finalization",
            max(0, len(plans) - 1),
        )
    if plans and len(plans) == len(plan_reviews) + 1:
        return "plan_reviewer", max(0, len(plans) - 1)
    if (root / "classification.json").is_file() and (root / "context.json").is_file():
        return (
            "planner"
            if manifest.tier.value in {"standard", "substantial"}
            else "implementation_worker"
        ), 0
    if not (root / "classification.json").exists():
        return "classifier", 0
    raise LegacyRecoveryError("old artifacts do not identify one next stage")


def _artifact_paths(store: RunStore) -> list[str]:
    return sorted(
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
        and path.name not in {"manifest.json", "events.jsonl", "claim.lock", "active-provider.json"}
        and "checkpoints" not in path.relative_to(store.root).parts
    )


def recover_legacy(
    repo: GitRepo,
    config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    *,
    adopt_worktree: bool,
) -> str:
    """Hash-pin one old artifact set, linking terminal quota failures to a new run."""
    source = RunStore(repo.root, run_id)
    with source.claim():
        manifest = source.read_manifest()
        if manifest.checkpoint is not None:
            return run_id
        if manifest.schema_version != "1.0":
            raise LegacyRecoveryError("run without a checkpoint is not a pre-feature run")
        if any((source.root / "checkpoints").glob("*.json")):
            raise LegacyRecoveryError("old run has orphan checkpoint files; recovery is ambiguous")
        terminal = manifest.status in {RunState.FAILED, RunState.ESCALATED}
        if terminal and not _confirmed_quota(source):
            raise LegacyRecoveryError("terminal old run has no confirmed provider quota rejection")
        if manifest.status in {
            RunState.COMPLETE_LOCAL,
            RunState.CANCELLED,
            RunState.BLOCKED,
            RunState.AWAITING_APPROVAL,
        }:
            raise LegacyRecoveryError(f"old run is {manifest.status}; it cannot recover")
        if source.active_provider_alive():
            raise LegacyRecoveryError("recorded provider process is still alive")
        if not manifest.config_layers or manifest.config_layers[0] != config.model_dump(
            mode="json"
        ):
            raise LegacyRecoveryError("project configuration changed")
        if manifest.approval_required and not (source.root / "approval/approved.json").is_file():
            raise LegacyRecoveryError("old run has no human approval proof")
        stage, cycle = _infer_next(source, config)
        worktree = Path(manifest.git.worktree or "")
        if not manifest.git.base_commit or not manifest.git.branch:
            raise LegacyRecoveryError("old run has no branch and base commit")
        if not worktree.exists():
            if repo.run_git("rev-parse", manifest.git.branch) != manifest.git.base_commit:
                raise LegacyRecoveryError("removed old worktree cannot be recreated from its base")
            worktree = worktree.parent / (run_id + "-recovered")
            repo.run_git("worktree", "add", "-q", str(worktree), manifest.git.branch)
        worktree_repo = GitRepo(worktree)
        if worktree_repo.current_branch() != manifest.git.branch:
            raise LegacyRecoveryError("old worktree branch changed")
        if worktree_repo.current_commit() != manifest.git.base_commit:
            raise LegacyRecoveryError("old worktree HEAD changed")
        enforce_fence(worktree_repo, manifest.git.base_commit, ScopeFence.from_config(config.scope))
        snapshot = capture_snapshot(worktree_repo, manifest.git.base_commit)
        if snapshot["entries"] and not adopt_worktree:
            raise LegacyRecoveryError(
                "old worktree has unverified partial edits; inspect and retry with "
                f"--adopt-worktree. Diff:\n{worktree_repo.change_diff(manifest.git.base_commit)}"
            )
        target = source
        if terminal:
            new_id = new_run_id(str(manifest.tier), f"recovered-{run_id[-20:]}")
            target = RunStore(repo.root, new_id)
            if target.root.exists():
                raise LegacyRecoveryError(f"linked run {new_id} already exists")
            shutil.copytree(source.root, target.root, ignore=shutil.ignore_patterns("claim.lock"))
            target._write_manifest(
                manifest.model_copy(
                    update={
                        "run_id": new_id,
                        "linked_source_run": run_id,
                        "status": RunState.PAUSED_INTERRUPTED,
                        "completed_at": None,
                        "terminal_reason": None,
                        "pause": None,
                        "auto_resume": False,
                        "git": GitBlock(
                            base_commit=manifest.git.base_commit,
                            branch=manifest.git.branch,
                            worktree=str(worktree),
                        ),
                    }
                )
            )
        elif worktree != Path(manifest.git.worktree or ""):
            target.update_manifest(
                git=GitBlock(
                    base_commit=manifest.git.base_commit,
                    branch=manifest.git.branch,
                    worktree=str(worktree),
                )
            )
        if not terminal:
            target.update_manifest(status=RunState.PAUSED_INTERRUPTED)
        hashes = input_hashes(target, registry)
        target.checkpoint(
            next_stage=stage,
            artifacts=_artifact_paths(target),
            worktree=snapshot,
            inputs=hashes,
            cycle=cycle,
        )
        target.append_event(
            {
                "event": "legacy_recovered",
                "source": run_id,
                "next_stage": stage,
                "adopted": bool(snapshot["entries"]),
            }
        )
        return target.run_id
