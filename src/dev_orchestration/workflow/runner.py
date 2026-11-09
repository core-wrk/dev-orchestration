"""Drive one local run through the M2 workflow and its safety gates."""

import hashlib
import json
import re
import subprocess
import tempfile
from contextlib import nullcontext, suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.adapters.base import AgentInterruptedError, AgentTimeoutError
from dev_orchestration.adapters.registry import ReadOnlyRoleUnsupportedError, RoleRegistry
from dev_orchestration.adapters.usage import UsageAwareAdapter, UsageLimitError, next_attempt
from dev_orchestration.artifacts.goals import validate_goal_id
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig, ValidationCommand
from dev_orchestration.config.resolver import PROTECTED_DEFAULTS
from dev_orchestration.context.assembler import Category, ContextContractError, PromptBudgetError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, RunState, Tier
from dev_orchestration.domain.findings import Verification, verification_allows_completion
from dev_orchestration.domain.run import (
    MAX_VERIFICATION_REPAIRS,
    GitBlock,
    PauseRecord,
    RoleBinding,
    StageActor,
    validation_repair_limit,
    verification_ask_available,
)
from dev_orchestration.git.repo import EmptySnapshotError, GitCommandError, GitRepo
from dev_orchestration.git.worktree import remove_worktree
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    bootstrap_run,
    classification_references_for_run,
    resolve_run_policy,
    resolve_runtime_context,
)
from dev_orchestration.workflow.checkpoints import record_stage
from dev_orchestration.workflow.contract import (
    CONTRACT_ARTIFACT,
    PRECHECK_NAME,
    NoSafeRepairScope,
    edited_paths,
    file_digests,
    is_test_path,
    load_contract,
    precheck,
    repair_edit_violations,
    review_repair_findings,
    review_repair_scope,
    save_contract,
    save_repair_scope,
    validation_repair_scope,
    verification_repair_scope,
    verification_route,
)
from dev_orchestration.workflow.criteria_commands import criteria_command_evidence
from dev_orchestration.workflow.dependencies import dependency_sources
from dev_orchestration.workflow.engine import Engine, IllegalTransitionError
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
from dev_orchestration.workflow.plan_reuse import (
    RECEIPT as PLAN_REVIEW_RECEIPT,
)
from dev_orchestration.workflow.plan_reuse import (
    create_receipt as create_plan_review_receipt,
)
from dev_orchestration.workflow.plan_reuse import (
    match_source as match_plan_review_source,
)
from dev_orchestration.workflow.plan_reuse import (
    validate_source as validate_plan_review_source,
)
from dev_orchestration.workflow.retry_feedback import (
    ARTIFACT as PRIOR_FAILURES_ARTIFACT,
)
from dev_orchestration.workflow.retry_feedback import (
    build_snapshot as build_prior_failures_snapshot,
)
from dev_orchestration.workflow.retry_feedback import (
    compare_validation,
)
from dev_orchestration.workflow.scheduler import SchedulerQueue
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    cleanup_ephemeral_paths,
    enforce_fence,
    enforce_ignored_writes,
)
from dev_orchestration.workflow.seed_patch import (
    PATCH_ARTIFACT as SEED_PATCH_ARTIFACT,
)
from dev_orchestration.workflow.seed_patch import (
    RECEIPT_ARTIFACT as SEED_PATCH_RECEIPT,
)
from dev_orchestration.workflow.seed_patch import (
    apply_seed_patch_once,
    save_seed_patch,
)
from dev_orchestration.workflow.snapshot import capture_snapshot
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    FindingIdentityAmbiguity,
    RemediationExhausted,
    classify,
    discover_dependencies,
    execute,
    plan,
    reconcile,
    remediate,
    repair_validation,
    repair_verification,
    review_implementation,
    review_plan,
    verify,
)
from dev_orchestration.workflow.tiers import (
    TierDowngradeError,
    UnsupportedTierError,
    specification_minimum_tier,
    stages_for,
)
from dev_orchestration.workflow.validation import (
    ValidationEnvironmentError,
    ValidationOutcome,
    all_passed,
    run_validations,
    validation_environment,
    validation_file_hashes,
)


class RunOutcome(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    run_id: str
    final_state: RunState
    store_root: Path
    verification: Verification | None = None
    reason: str = ""


def _tier_rank(tier: Tier) -> int:
    return {
        Tier.TRIVIAL: 0,
        Tier.STANDARD: 1,
        Tier.SUBSTANTIAL: 2,
        Tier.HIGH_RISK: 3,
    }[tier]


def _evidence(outcomes: list[ValidationOutcome]) -> str:
    if not outcomes:
        return "No required validation commands were resolved for this tier."
    return "\n".join(
        f"{outcome.name}: exit {outcome.exit_code} "
        f"({'PASS' if outcome.passed else 'FAIL'})\n"
        f"stdout: {outcome.stdout_tail}\n"
        f"stderr: {outcome.stderr_tail}"
        for outcome in outcomes
    )


def _criteria_command_evidence(
    repo: GitRepo,
    worktree_repo: GitRepo,
    base_commit: str,
    project_config: ProjectConfig,
    criteria: list[str],
) -> str:
    """Run commands quoted in the criteria, refusing any that change the worktree."""
    cleanup_ephemeral_paths(worktree_repo.root)
    before = capture_snapshot(worktree_repo, base_commit)
    with validation_environment(
        repo.root,
        worktree_repo.root,
        tuple(project_config.validation_links),
        tuple(project_config.required_validation_links),
    ):
        evidence = criteria_command_evidence(
            criteria, project_config.validation, worktree_repo.root
        )
    cleanup_ephemeral_paths(worktree_repo.root)
    if capture_snapshot(worktree_repo, base_commit) != before:
        raise ContextContractError(
            "a command named in the acceptance criteria changed the worktree"
        )
    return f"\n\n{evidence}" if evidence else ""


def _validation_json(outcomes: list[ValidationOutcome]) -> list[dict]:
    return [outcome.model_dump(mode="json") for outcome in outcomes]


def _dependency_review_identity(store: RunStore) -> dict | None:
    path = store.root / "dependency-report.json"
    if not path.is_file():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_inventory_identity": report.get("source_inventory_identity"),
    }


def _save_validation(store: RunStore, outcomes: list[ValidationOutcome], cycle: int = 0) -> str:
    versions = []
    for path in (store.root / "execution").glob("validation-v*.json"):
        try:
            versions.append(int(path.stem.rsplit("-v", 1)[1]))
        except ValueError:
            continue
    artifact = f"execution/validation-v{max(versions, default=0) + 1}.json"
    records = _validation_json(outcomes)
    input_hashes = store.read_manifest().validation_input_hashes
    store.write_json_artifact(artifact, records, immutable=True)
    store.update_manifest(validation_artifact=artifact)
    store.append_event(
        {
            "event": "validated",
            "cycle": cycle,
            "passed": all_passed(outcomes),
            "required_commands": [outcome.name for outcome in outcomes],
            "input_hashes": input_hashes,
            "detail": (
                "No required validation commands were resolved for this tier."
                if not outcomes
                else ""
            ),
            "outcomes": records,
        }
    )
    prior_path = store.root / PRIOR_FAILURES_ARTIFACT
    if store.read_manifest().retry_of is not None and prior_path.is_file():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        for item in compare_validation(prior, store):
            store.append_event({"event": "validation_failure_repeated", **item})
    return artifact


def _approval_required(
    project_config: ProjectConfig,
    tier: Tier,
    active_profiles: list[str],
) -> bool:
    """Resolve the human gate without allowing a provider to bypass it."""
    if tier is Tier.HIGH_RISK:
        return project_config.approval.high_risk
    if tier is not Tier.SUBSTANTIAL or project_config.approval.substantial:
        return tier is Tier.SUBSTANTIAL and project_config.approval.substantial
    for profile_name in active_profiles:
        profile = project_config.profiles.definitions.get(profile_name)
        if profile is None:
            profile = project_config.profiles.definitions.get(profile_name.replace("-", "_"))
        if profile and profile.controls.get("human_approval") in {True, "required"}:
            return True
    return False


def _approval_summary(
    request_text: str,
    project_config: ProjectConfig,
    tier: Tier,
    active_profiles: list[str],
    approved_plan: str,
) -> str:
    protected = {
        "autonomous_push": False,
        "autonomous_merge": False,
        "autonomous_deploy": False,
        "autonomous_production_data_mutation": False,
    }
    return (
        "# Human approval required\n\n"
        "Execution is paused until a human explicitly approves this run.\n\n"
        f"## Request\n{request_text}\n\n"
        f"## Tier\n{tier}\n\n"
        f"## Active profiles\n{', '.join(active_profiles) or '(none)'}\n\n"
        f"## Scope\ninclude: {json.dumps(project_config.scope.include)}\n"
        f"exclude: {json.dumps(project_config.scope.exclude)}\n\n"
        f"## Protected constraints\n{json.dumps(protected, sort_keys=True)}\n\n"
        "## Approved plan\n"
        f"{approved_plan}\n\n"
        "## Decision\n"
        "Record an explicit human approval before execution may begin.\n"
    )


def _record_roles(store, registry: RoleRegistry) -> None:
    roles = {}
    for role, binding in registry.roles.items():
        adapter = registry.adapters.get(binding.adapter)
        binary = getattr(adapter, "binary", None)
        resolve_model = getattr(adapter, "model_id", None)
        roles[role] = RoleBinding(
            adapter=binding.adapter,
            model_alias=binding.model,
            model_id=resolve_model(binding.model) if callable(resolve_model) else binding.model,
            reasoning=binding.reasoning,
            timeout_seconds=binding.timeout_seconds,
            resolved_binary=str(binary) if binary is not None else None,
        )
    store.update_manifest(roles=roles)
    store.write_json_artifact(
        "roles.json", {role: value.model_dump(mode="json") for role, value in roles.items()}
    )


def _task_contract(
    request_text: str,
    project_config: ProjectConfig,
    criteria: list[str],
    invariants: list[ContextRef],
) -> str:
    protected = {
        "protected.autonomous_push": False,
        "protected.autonomous_merge": False,
        "protected.autonomous_deploy": False,
        "protected.autonomous_production_data_mutation": False,
    }
    return (
        "# Task Contract\n\n"
        f"## Request\n{request_text}\n\n"
        f"## Scope\ninclude: {json.dumps(project_config.scope.include)}\n"
        f"exclude: {json.dumps(project_config.scope.exclude)}\n\n"
        f"## Scope fence\n{json.dumps({'include': project_config.scope.include, 'exclude': project_config.scope.exclude}, sort_keys=True)}\n\n"
        f"## Acceptance criteria\n{chr(10).join(f'- {item}' for item in criteria)}\n\n"
        f"## Protected constraints\n{json.dumps(protected, sort_keys=True)}\n\n"
        f"## Repository invariants\n{chr(10).join(item.content for item in invariants)}\n"
    )


def _persist_task_contract(store: RunStore, content: str) -> Path:
    """Persist a trivial contract idempotently before checkpointing implementation."""
    relative = "planning/task-contract.md"
    target = store.root / relative
    manifest = store.read_manifest()
    current = target
    while current != store.repo_root:
        if current.is_symlink():
            raise ContextContractError("saved trivial task contract path contains a symlink")
        parent = current.parent
        if parent == current:
            break
        current = parent
    if target.exists():
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            raise ContextContractError("saved trivial task contract does not match this run")
    elif manifest.plan_origin == "task_contract":
        raise ContextContractError("saved trivial task contract is missing; start a new run")
    else:
        store.write_text_artifact(relative, content, immutable=True)
    store.update_manifest(plan_origin="task_contract", approved_plan_version="task-contract")
    events_path = store.root / "events.jsonl"
    if not any(
        json.loads(line).get("event") == "task_contract_written"
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ):
        store.append_event({"event": "task_contract_written"})
    return target


def _terminal(
    engine: Engine,
    state: RunState,
    reason: str,
    cause: str = "other",
    failure_class: str | None = None,
    failure_signature: str | None = None,
    *,
    stop_code: str | None = None,
) -> None:
    store = engine.store
    is_failure = state in {RunState.FAILED, RunState.ESCALATED}
    if is_failure:
        if failure_class is None:
            if cause == "environment":
                failure_class = "environment"
            elif cause in {
                "implementation_review_escalated",
                "scope_violation",
                "safety_escalation",
                "restart_governance_blocked",
            }:
                failure_class = "human_decision"
            else:
                # Review exhaustion is a product defect and independently requires
                # a human resolution through the existing governance cause.
                failure_class = "product"
        normalized = reason.replace(store.run_id, "<run>")
        normalized = re.sub(
            r"(?:/private)?/(?:tmp|var/folders)/[^\s:]+", "<temporary-path>", normalized
        )
        normalized = re.sub(r"\b\d{8}-\d{6}_[a-z]+_[a-z0-9_-]+\b", "<run>", normalized)
        normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}T[^\s]+", "<time>", normalized)
        failure_signature = failure_signature or f"{cause}:{normalized.strip().lower()}"
    else:
        failure_class = None
        failure_signature = None
    store.update_manifest(
        terminal_reason=reason,
        terminal_cause=cause,
        failure_class=failure_class,
        failure_reason=reason if is_failure else None,
        failure_signature=failure_signature,
    )
    store.append_event(
        {
            "event": "run_terminal",
            "state": str(state),
            "detail": reason,
            "cause": cause,
            "failure_class": failure_class,
            "failure_signature": failure_signature,
        }
    )
    if engine.state not in {
        RunState.COMPLETE_LOCAL,
        RunState.ESCALATED,
        RunState.FAILED,
        RunState.CANCELLED,
    }:
        engine.transition(state, stop_code=stop_code)


def _deliverable_patch(worktree_repo: GitRepo, base_commit: str) -> bytes:
    """Return the binary-safe patch representing every visible change from the base."""
    return worktree_repo.change_patch_bytes(base_commit)


def _patch_digest(patch: bytes) -> str:
    return hashlib.sha256(patch).hexdigest()


def _record_no_progress(
    engine: Engine,
    *,
    cause: str,
    stage: str,
    before: bytes,
    after: bytes,
    cycle: int | None = None,
    missing_test: bool = False,
) -> None:
    """Record a stable no-progress failure while leaving any partial patch recoverable.

    ``missing_test`` marks an evidence repair that changed files but no test file.
    """
    is_empty = not after
    unchanged = before == after
    artifact_name = (
        "implementation-worker.json"
        if stage == "implementation_worker"
        else f"verification-repair-v{cycle}.json"
        if stage == "verification_repair"
        else f"remediation-v{cycle}.json"
    )
    artifact = f"execution/no-progress/{artifact_name}"
    engine.store.write_json_artifact(
        artifact,
        {
            "cause": cause,
            "stage": stage,
            "cycle": cycle,
            "comparison": "no_test_change"
            if missing_test
            else "empty_change_patch"
            if is_empty
            else "unchanged_change_patch",
            "comparison_format": "git-change-patch-bytes",
            "changed": not unchanged,
            "before_digest": _patch_digest(before),
            "after_digest": _patch_digest(after),
            "before_bytes": len(before),
            "after_bytes": len(after),
        },
        immutable=True,
    )
    engine.store.append_event(
        {"event": "no_progress_detected", "cause": cause, "stage": stage, "artifact": artifact}
    )
    reason = (
        "implementation worker produced no deliverable changes"
        if cause == "worker_no_changes"
        else f"evidence repair round {cycle} changed no test file"
        if missing_test
        else f"verification repair round {cycle} produced no deliverable progress"
        if stage == "verification_repair"
        else f"implementation remediation cycle {cycle} produced no deliverable progress"
    )
    signature = (
        f"{cause}:empty-change-set"
        if cause == "worker_no_changes"
        else f"{cause}:{_patch_digest(after)}"
    )
    _terminal(
        engine,
        RunState.FAILED,
        reason,
        cause,
        failure_class="product",
        failure_signature=signature,
    )


# COMPLETE_LOCAL is deliberately excluded from cleanup: that worktree and its
# branch are the run's deliverable, left for a human to review, diff, and
# merge by hand. Only the terminal states that produced nothing worth keeping
# checked out are swept.
_UNPRODUCTIVE_TERMINAL_STATES = frozenset({RunState.FAILED, RunState.ESCALATED, RunState.CANCELLED})

FAILURE_PATCH_ARTIFACT = "execution/failure-change.patch"
FAILURE_BUNDLE_ARTIFACT = "execution/failure-branch.bundle"
CLEANUP_RECEIPT_ARTIFACT = "execution/cleanup-receipt.json"


def _archive_failed_worktree(engine: Engine, repo: GitRepo, worktree: Path) -> dict:
    """Save and verify all source changes and any run-created commit history."""
    manifest = engine.store.read_manifest()
    base = manifest.git.base_commit
    branch = manifest.git.branch
    recorded = manifest.git.worktree
    if not base or not branch or not recorded:
        raise GitCommandError("run record lacks branch, base commit, or worktree ownership")
    if Path(recorded).resolve() != worktree.resolve():
        raise GitCommandError("cleanup path does not match the worktree recorded by this run")

    worktree_repo = GitRepo(worktree)
    if worktree_repo.current_branch() != branch:
        raise GitCommandError("worktree is no longer on the branch recorded by this run")
    if worktree_repo.ignored_paths():
        raise GitCommandError("worktree contains ignored files; inspect them before cleanup")
    head = worktree_repo.current_commit()
    if repo.branch_head(branch) != head:
        raise GitCommandError("run branch moved independently of its recorded worktree")
    patch = worktree_repo.change_patch_bytes(base)
    if not worktree_repo.verify_patch(base, patch):
        raise GitCommandError("saved change patch failed its scratch-index restoration check")

    patch_path = engine.store.root / FAILURE_PATCH_ARTIFACT
    legacy_patch_path = engine.store.root / CHANGE_PATCH_ARTIFACT
    if patch_path.exists() and patch_path.read_bytes() != patch:
        raise GitCommandError("existing failure patch differs from current worktree changes")
    if not patch_path.exists():
        engine.store.write_bytes_artifact(FAILURE_PATCH_ARTIFACT, patch, immutable=True)
    if legacy_patch_path.exists() and legacy_patch_path.read_bytes() != patch:
        raise GitCommandError("existing continuation patch differs from current worktree changes")
    if not legacy_patch_path.exists():
        engine.store.write_bytes_artifact(CHANGE_PATCH_ARTIFACT, patch, immutable=True)

    bundle = b""
    if head != base:
        if not worktree_repo.is_ancestor(base):
            raise GitCommandError("run branch no longer descends from its recorded base")
        bundle_path = engine.store.root / FAILURE_BUNDLE_ARTIFACT
        if bundle_path.exists():
            bundle = bundle_path.read_bytes()
            with tempfile.TemporaryDirectory() as scratch:
                check = Path(scratch) / "branch.bundle"
                check.write_bytes(bundle)
                check_repo = GitRepo(repo.root)
                listed = check_repo.run_git("bundle", "list-heads", str(check))
                if f"{head} refs/heads/{branch}" not in listed:
                    raise GitCommandError(
                        "existing branch bundle does not match the recorded branch"
                    )
                check_repo.run_git("bundle", "verify", str(check))
        else:
            with tempfile.TemporaryDirectory() as scratch:
                bundle = worktree_repo.bundle_branch(branch, base, Path(scratch) / "branch.bundle")
            engine.store.write_bytes_artifact(FAILURE_BUNDLE_ARTIFACT, bundle, immutable=True)

    receipt = {
        "run_id": manifest.run_id,
        "base_commit": base,
        "branch": branch,
        "branch_head": head,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "bundle_sha256": hashlib.sha256(bundle).hexdigest() if bundle else None,
        "patch_artifact": FAILURE_PATCH_ARTIFACT,
        "bundle_artifact": FAILURE_BUNDLE_ARTIFACT if bundle else None,
    }
    receipt_path = engine.store.root / CLEANUP_RECEIPT_ARTIFACT
    if receipt_path.exists():
        if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
            raise GitCommandError("existing cleanup receipt does not match archived work")
    else:
        engine.store.write_json_artifact(CLEANUP_RECEIPT_ARTIFACT, receipt, immutable=True)
    engine.store.append_event(
        {
            "event": "failure_archive_saved",
            "patch_sha256": receipt["patch_sha256"],
            "bundle_sha256": receipt["bundle_sha256"],
        }
    )
    return receipt


def _verify_cleanup_receipt(engine: Engine, repo: GitRepo, receipt: dict) -> None:
    manifest = engine.store.read_manifest()
    if (
        receipt.get("run_id") != manifest.run_id
        or receipt.get("base_commit") != manifest.git.base_commit
        or receipt.get("branch") != manifest.git.branch
    ):
        raise GitCommandError("cleanup receipt does not match the recorded run ownership")
    patch_path = engine.store.root / FAILURE_PATCH_ARTIFACT
    if not patch_path.is_file():
        raise GitCommandError("archived failure patch is missing")
    patch = patch_path.read_bytes()
    if hashlib.sha256(patch).hexdigest() != receipt.get("patch_sha256"):
        raise GitCommandError("archived failure patch hash does not match its receipt")
    if not repo.verify_patch(receipt["base_commit"], patch):
        raise GitCommandError("archived failure patch no longer restores its recorded changes")
    bundle_digest = receipt.get("bundle_sha256")
    if bundle_digest is not None:
        bundle_path = engine.store.root / FAILURE_BUNDLE_ARTIFACT
        if not bundle_path.is_file():
            raise GitCommandError("archived branch bundle is missing")
        bundle = bundle_path.read_bytes()
        if hashlib.sha256(bundle).hexdigest() != bundle_digest:
            raise GitCommandError("archived branch bundle hash does not match its receipt")
        with tempfile.TemporaryDirectory() as scratch:
            check = Path(scratch) / "branch.bundle"
            check.write_bytes(bundle)
            output = repo.run_git("bundle", "list-heads", str(check))
            expected = f"{receipt['branch_head']} refs/heads/{receipt['branch']}"
            if expected not in output:
                raise GitCommandError("archived branch bundle has a different commit")
            repo.run_git("bundle", "verify", str(check))


def cleanup_worktree_if_unproductive(
    engine: Engine, repo: GitRepo, worktree: Path | None = None
) -> None:
    """Archive run changes, then remove only the recorded run worktree and branch."""
    if engine.state not in _UNPRODUCTIVE_TERMINAL_STATES:
        return
    manifest = engine.store.read_manifest()
    recorded = Path(manifest.git.worktree) if manifest.git.worktree else None
    if recorded is None:
        engine.store.append_event(
            {"event": "run_cleanup_blocked", "detail": "run has no recorded worktree ownership"}
        )
        return
    if worktree is not None and worktree.resolve() != recorded.resolve():
        engine.store.append_event(
            {
                "event": "run_cleanup_blocked",
                "detail": "requested path differs from recorded worktree",
            }
        )
        return
    worktree = recorded
    try:
        receipt = None
        if worktree.exists():
            receipt = _archive_failed_worktree(engine, repo, worktree)
            _verify_cleanup_receipt(engine, repo, receipt)
            # Recheck ownership and exact patch bytes immediately before removal.
            current = GitRepo(worktree)
            if current.current_branch() != receipt["branch"]:
                raise GitCommandError("worktree branch changed after archive verification")
            if current.current_commit() != receipt["branch_head"]:
                raise GitCommandError("worktree commit changed after archive verification")
            if current.ignored_paths():
                raise GitCommandError("ignored files appeared after archive verification")
            patch = current.change_patch_bytes(receipt["base_commit"])
            if hashlib.sha256(patch).hexdigest() != receipt["patch_sha256"]:
                raise GitCommandError("worktree changes changed after archive verification")
            remove_worktree(repo, worktree)
            engine.store.append_event({"event": "worktree_removed", "path": str(worktree)})
        else:
            receipt_path = engine.store.root / CLEANUP_RECEIPT_ARTIFACT
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            else:
                event_path = engine.store.root / "events.jsonl"
                recorded_events = (
                    [
                        json.loads(line)
                        for line in event_path.read_text(encoding="utf-8").splitlines()
                    ]
                    if event_path.is_file()
                    else []
                )
                if any(
                    event.get("event") == "run_bootstrapped" for event in recorded_events
                ) or not any(event.get("event") == "bootstrap_failed" for event in recorded_events):
                    raise GitCommandError(
                        "missing worktree has no archive receipt from a failed bootstrap"
                    )
                reservation = next(
                    (
                        event
                        for event in recorded_events
                        if event.get("event") == "run_branch_reserved"
                    ),
                    None,
                )
                base = manifest.git.base_commit
                branch = manifest.git.branch
                if (
                    reservation is None
                    or reservation.get("branch_existed_before") is not False
                    or reservation.get("worktree_existed_before") is not False
                    or not base
                    or not branch
                ):
                    raise GitCommandError("missing worktree has no bootstrap ownership receipt")
                head = repo.branch_head(branch) if repo.branch_exists(branch) else base
                if head != base:
                    raise GitCommandError(
                        "missing worktree has committed changes but no preservation bundle"
                    )
                patch = b""
                if not repo.verify_patch(base, patch):
                    raise GitCommandError("empty bootstrap patch failed its restoration check")
                for artifact in (FAILURE_PATCH_ARTIFACT, CHANGE_PATCH_ARTIFACT):
                    artifact_path = engine.store.root / artifact
                    if artifact_path.exists():
                        if artifact_path.read_bytes() != patch:
                            raise GitCommandError(f"existing artifact {artifact} is not empty")
                    else:
                        engine.store.write_bytes_artifact(artifact, patch, immutable=True)
                receipt = {
                    "run_id": manifest.run_id,
                    "base_commit": base,
                    "branch": branch,
                    "branch_head": head,
                    "patch_sha256": hashlib.sha256(patch).hexdigest(),
                    "bundle_sha256": None,
                    "patch_artifact": FAILURE_PATCH_ARTIFACT,
                    "bundle_artifact": None,
                }
                if receipt_path.exists():
                    if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
                        raise GitCommandError(
                            "existing cleanup receipt differs from bootstrap state"
                        )
                else:
                    engine.store.write_json_artifact(
                        CLEANUP_RECEIPT_ARTIFACT, receipt, immutable=True
                    )

        _verify_cleanup_receipt(engine, repo, receipt)

        branch = manifest.git.branch
        if not branch:
            raise GitCommandError("run record lacks its owned branch name")
        if repo.branch_exists(branch):
            if repo.branch_head(branch) != receipt["branch_head"]:
                raise GitCommandError("run branch changed after its archive was verified")
            repo.delete_branch(branch)
        engine.store.append_event({"event": "run_cleanup_complete", "branch": branch})
    except (GitCommandError, OSError, ValueError, KeyError, TypeError) as exc:
        engine.store.append_event({"event": "run_cleanup_blocked", "detail": str(exc)})


# Causes where the plan was fine and the code was close: the change is kept so a retry
# can continue from it instead of re-implementing.
CHANGE_PATCH_ARTIFACT = "execution/final-change.patch"
_PATCH_WORTHY_CAUSES = frozenset(
    {
        "implementation_review_exhausted",
        "implementation_review_escalated",
        "remediation_no_progress",
        "validation_failed",
        "validation_failed_after_remediation",
    }
)


CONTINUED_CHANGE_ARTIFACT = "execution/continued-change.patch"


def apply_continued_change(
    store: RunStore, worktree_repo: GitRepo, base_commit: str, fence: ScopeFence
) -> bool:
    """Apply a saved continuation only after verifying its checkpointed bytes."""
    patch_path = store.root / CONTINUED_CHANGE_ARTIFACT
    if not patch_path.is_file():
        return False
    receipt = store.verify_checkpoint()
    expected = receipt["artifacts"].get(CONTINUED_CHANGE_ARTIFACT)
    patch_bytes = patch_path.read_bytes()
    if expected is None or hashlib.sha256(patch_bytes).hexdigest() != expected:
        raise CheckpointError(
            f"checkpoint artifact {CONTINUED_CHANGE_ARTIFACT} changed or is missing"
        )
    _run_worktree_stage(
        worktree_repo,
        base_commit,
        fence,
        lambda: worktree_repo.apply_patch_bytes(patch_bytes),
    )
    return True


def _save_change_patch(engine: Engine, worktree: Path) -> None:
    store = engine.store
    manifest = store.read_manifest()
    if (
        engine.state not in _UNPRODUCTIVE_TERMINAL_STATES
        or manifest.terminal_cause not in _PATCH_WORTHY_CAUSES
        or manifest.git.base_commit is None
        or not worktree.exists()
    ):
        return
    try:
        patch = GitRepo(worktree).change_patch(manifest.git.base_commit)
        if patch.strip():
            store.write_text_artifact(CHANGE_PATCH_ARTIFACT, patch, immutable=True)
            store.append_event({"event": "change_patch_saved", "artifact": CHANGE_PATCH_ARTIFACT})
    except (GitCommandError, OSError, UnicodeError) as exc:
        store.append_event({"event": "change_patch_failed", "detail": str(exc)})


def _outcome(
    engine: Engine,
    verification: Verification | None = None,
    reason: str | None = None,
    *,
    repo: GitRepo | None = None,
    worktree: Path | None = None,
) -> RunOutcome:
    if repo is not None and worktree is not None:
        _save_change_patch(engine, worktree)
        cleanup_worktree_if_unproductive(engine, repo, worktree)
    manifest = engine.store.read_manifest()
    return RunOutcome(
        run_id=manifest.run_id,
        final_state=manifest.status,
        store_root=engine.store.root,
        verification=verification,
        reason=reason if reason is not None else manifest.terminal_reason or "",
    )


def _implementation_review_required(tier: Tier, validation: dict[str, ValidationCommand]) -> bool:
    """Whether this run must pass an independent implementation review.

    A tier path omits the review on the premise that deterministic validation covers
    the same ground more cheaply. Where no validation command actually runs at this
    tier that premise is false and the review is the only check standing between the
    worker and a completed run, so it is kept regardless of the stage path.
    """
    if "implementation_review" in stages_for(tier):
        return True
    return not any(tier in spec.required_for for spec in validation.values())


def _effective_tier(
    classification_tier: Tier,
    override: Tier | None,
    minimum: Tier | None,
    downgrade_reason: str | None = None,
) -> Tier:
    requested = classification_tier
    if override is not None:
        # A downgrade is permitted, but never silently: the classifier reads the request
        # text alone and over-classifies when a change merely *discusses* a sensitive
        # system. Requiring a recorded reason keeps the correction auditable instead of
        # making over-classification unrecoverable.
        if _tier_rank(override) < _tier_rank(classification_tier) and not (
            downgrade_reason and downgrade_reason.strip()
        ):
            raise TierDowngradeError(
                f"--tier {override} is below the classified tier {classification_tier}; "
                "pass --downgrade-reason to record why the lower tier is justified"
            )
        requested = override
    if minimum and _tier_rank(requested) < _tier_rank(minimum):
        requested = minimum
    return requested


def _with_precheck(
    outcomes: list[ValidationOutcome],
    contract: dict | None,
    worktree_repo: GitRepo,
    base_commit: str,
) -> list[ValidationOutcome]:
    """Add a failed pseudo-check when a passing run still breaks its saved contract."""
    if contract is None or not all_passed(outcomes):
        return outcomes
    failures = precheck(contract, worktree_repo.change_inventory(base_commit), worktree_repo.root)
    if not failures:
        return outcomes
    return [
        *outcomes,
        ValidationOutcome(
            name=PRECHECK_NAME,
            command="acceptance contract pre-check",
            exit_code=1,
            passed=False,
            stdout_tail="",
            stderr_tail="; ".join(failures),
        ),
    ]


def _run_worktree_stage(
    worktree_repo: GitRepo,
    base_commit: str,
    fence: ScopeFence,
    action,
    links: tuple[Path, tuple[str, ...]] | None = None,
):
    """Run an agent stage and restore all ignored side effects afterward.

    ``links`` (base checkout, relative paths) exposes the validation links, such as ``.venv``,
    for the length of the stage, so a worker can run the validation commands it is asked to
    self-check with. They are removed before the fence and ignored-write checks.
    """
    ignored_before = worktree_repo.ignored_paths()
    try:
        if links is None:
            result = action()
        else:
            with validation_environment(links[0], worktree_repo.root, links[1]):
                result = action()
        enforce_fence(worktree_repo, base_commit, fence)
        enforce_ignored_writes(worktree_repo, ignored_before, fence)
    except Exception:
        try:
            enforce_fence(worktree_repo, base_commit, fence)
            enforce_ignored_writes(worktree_repo, ignored_before, fence)
        finally:
            worktree_repo.restore_ignored_snapshot(ignored_before)
            cleanup_ephemeral_paths(worktree_repo.root)
        raise
    worktree_repo.restore_ignored_snapshot(ignored_before)
    cleanup_ephemeral_paths(worktree_repo.root)
    return result


def _run_validation_stage(
    worktree_repo: GitRepo,
    base_commit: str,
    fence: ScopeFence,
    action,
    validation_root: Path | None = None,
    link_paths: tuple[str, ...] = (".venv",),
    required_paths: tuple[str, ...] = (),
    validation_files: tuple[str, ...] = (),
    expected_input_hashes: dict[str, str] | None = None,
    store: RunStore | None = None,
):
    """Run validation while ensuring its ignored caches do not become residue."""
    timing = (
        store.stage_operation("validation", StageActor(role="validator"))
        if store
        else nullcontext()
    )
    with timing:
        ignored_before = worktree_repo.ignored_paths()
        try:
            if validation_files:
                actual = validation_file_hashes(
                    GitRepo(validation_root or worktree_repo.root), list(validation_files)
                )
                if expected_input_hashes is not None and actual != expected_input_hashes:
                    raise ValidationEnvironmentError(
                        "declared validation files changed during the run; restart from validation"
                    )
            with validation_environment(
                validation_root, worktree_repo.root, link_paths, required_paths, validation_files
            ):
                result = action()
            enforce_fence(worktree_repo, base_commit, fence)
            return result
        finally:
            worktree_repo.restore_ignored_snapshot(ignored_before)
            cleanup_ephemeral_paths(worktree_repo.root)


def execute_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    request_text: str,
    worktree_root: Path,
    acceptance_criteria: list[str],
    *,
    goal_id: str,
    tier_override: Tier | None = None,
    downgrade_reason: str | None = None,
    external_plan: Path | None = None,
    acceptance_criteria_source: str = "explicit",
    auto_resume: bool = False,
    scheduler_queue: SchedulerQueue | None = None,
    retry_of: str | None = None,
    retry_source: RunStore | None = None,
    continue_patch: Path | None = None,
    retry_resolution_reason: str | None = None,
    reviewed_plan_from: RunStore | None = None,
    seed_patch: Path | None = None,
    manual_start: bool = False,
) -> RunOutcome:
    """Execute a run; every started run is returned in a terminal state."""
    if not request_text.strip():
        raise ContextContractError("a run requires non-empty request text")
    try:
        seed_patch_bytes = seed_patch.read_bytes() if seed_patch is not None else None
    except OSError as exc:
        raise ContextContractError(f"cannot read seed patch {seed_patch}: {exc}") from exc
    goal_id = validate_goal_id(goal_id)
    acceptance_criteria = _normalize_criteria(acceptance_criteria)
    if acceptance_criteria_source not in {"explicit", "request_fallback"}:
        raise ContextContractError(
            f"unknown acceptance criteria source {acceptance_criteria_source!r}"
        )
    if tier_override is not None:
        stages_for(tier_override)
    if retry_of is not None and retry_source is not None:
        if retry_source.run_id != retry_of:
            raise ContextContractError("retry evidence source does not match retry_of")
        if retry_source.read_manifest().goal_id != goal_id:
            raise ContextContractError("retry must inherit the source run's goal ID")
        if GitRepo(retry_source.repo_root).shared_root() != repo.shared_root():
            raise ContextContractError("retry evidence source belongs to a different repository")
    if (
        reviewed_plan_from is not None
        and GitRepo(reviewed_plan_from.repo_root).shared_root() != repo.shared_root()
    ):
        raise ContextContractError("reviewed plan source belongs to a different repository")
    reuse_source = reviewed_plan_from or (retry_source if retry_of is not None else None)
    source_finding_index = {}
    if reuse_source is not None:
        validate_plan_review_source(reuse_source)
        source_finding_index = reuse_source.finding_index()
    store, worktree = bootstrap_run(
        repo,
        project_config,
        request_text,
        Tier.STANDARD,
        worktree_root,
        goal_id,
        decision_resolution=(retry_of, retry_resolution_reason)
        if retry_of is not None and retry_resolution_reason
        else None,
        manual_start=manual_start or retry_of is not None,
        supplied_edit=seed_patch is not None,
    )
    if retry_of is not None:
        store.update_manifest(retry_of=retry_of)
    engine = Engine(store)
    fence = ScopeFence.from_config(project_config.scope)
    base_commit = store.read_manifest().git.base_commit
    if base_commit is None:
        _terminal(engine, RunState.FAILED, "bootstrap did not record a base commit")
        return _outcome(engine, repo=repo, worktree=worktree)
    claim = store.claim()
    claim.__enter__()
    try:
        worktree_repo = GitRepo(worktree)
        registry = RoleRegistry(
            registry.roles,
            {
                name: UsageAwareAdapter(adapter, auto_resume=auto_resume, store=store)
                for name, adapter in registry.adapters.items()
            },
        )
        store.update_manifest(auto_resume=auto_resume)
        if retry_resolution_reason:
            store.append_event(
                {"event": "retry_resolution_reason", "detail": retry_resolution_reason}
            )
        if seed_patch_bytes is not None:
            digest = save_seed_patch(store, seed_patch_bytes)
            store.update_manifest(seed_patch_artifact=SEED_PATCH_ARTIFACT, seed_patch_sha256=digest)
        if continue_patch is not None:
            store.write_bytes_artifact(
                CONTINUED_CHANGE_ARTIFACT,
                continue_patch.read_bytes(),
                immutable=True,
            )

        def receipt(next_stage: str, artifacts: list[str], cycle: int = 0) -> None:
            if store.read_manifest().seed_patch_artifact:
                artifacts = [*artifacts, SEED_PATCH_ARTIFACT, SEED_PATCH_RECEIPT]
            record_stage(
                store,
                registry,
                worktree,
                next_stage=next_stage,
                artifacts=artifacts,
                cycle=cycle,
            )

        def validate() -> list[ValidationOutcome]:
            outcomes = _run_validation_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: run_validations(project_config.validation, effective_tier, worktree),
                repo.root,
                tuple(project_config.validation_links),
                tuple(project_config.required_validation_links),
                tuple(project_config.validation_files),
                store.read_manifest().validation_input_hashes,
                store=store,
            )
            # After the validation environment is torn down, so only real changes are seen.
            return _with_precheck(outcomes, contract, worktree_repo, base_commit)

        def saved(scope):
            """Save a repair description before the checkpoint that precedes its worker."""
            return scope, save_repair_scope(store, scope)

        worker_links = (repo.root, tuple(project_config.validation_links))

        def guarded_repair(scope, call):
            """Run a repair worker and refuse edits beyond the files it was allowed."""
            before = file_digests(worktree, worktree_repo.change_inventory(base_commit))
            result = _run_worktree_stage(worktree_repo, base_commit, fence, call, worker_links)
            stray = (
                repair_edit_violations(
                    worktree, before, worktree_repo.change_inventory(base_commit), scope
                )
                if scope is not None
                else []
            )
            if stray:
                raise ScopeViolation(
                    f"{scope['gate']} repair changed files it was not allowed to change: {stray}"
                )
            return result

        def repair_failed_validation(
            outcomes: list[ValidationOutcome], cycle: int, next_stage: str
        ) -> tuple[list[ValidationOutcome], bool]:
            repaired_here = False
            while True:
                if all_passed(outcomes):
                    artifact = store.read_manifest().validation_artifact
                    if artifact is None:
                        raise CheckpointError("passed validation has no saved artifact pointer")
                    receipt(next_stage, [artifact], cycle)
                    return outcomes, False
                current = store.read_manifest()
                reserved = current.validation_repairs_reserved
                if reserved >= validation_repair_limit(current, cycle):
                    if repaired_here:
                        artifact = store.read_manifest().validation_artifact
                        receipt(next_stage, [artifact], cycle)
                    return outcomes, True
                round_number = reserved + 1
                scope, scope_artifact = saved(
                    validation_repair_scope(outcomes, round_number, fence)
                )
                store.update_manifest(validation_repairs_reserved=round_number)
                failed_artifact = store.read_manifest().validation_artifact
                if failed_artifact is None:
                    raise CheckpointError("failed validation has no saved artifact pointer")
                receipt("validation_repair", [failed_artifact, scope_artifact], cycle)
                engine.transition(RunState.REMEDIATION)
                guarded_repair(
                    scope,
                    lambda scope=scope, round_number=round_number, outcomes=outcomes: (
                        repair_validation(
                            registry,
                            approved,
                            worktree_repo.change_diff(base_commit),
                            outcomes,
                            worktree,
                            store,
                            dependency_report,
                            contract,
                            scope,
                            round_number,
                        )
                    ),
                )
                receipt("validation", [f"execution/validation-repair-v{round_number}.json"], cycle)
                engine.transition(RunState.VALIDATING)
                outcomes = validate()
                _save_validation(store, outcomes, cycle)
                repaired_here = True

        store.update_manifest(
            acceptance_criteria=acceptance_criteria,
            acceptance_criteria_source=acceptance_criteria_source,
            tier_override=tier_override,
            downgrade_reason=downgrade_reason,
        )
        _record_roles(store, registry)
        store.update_manifest(
            available_profiles=project_config.profiles.available,
            config_layers=[project_config.model_dump(mode="json")],
        )
        if retry_of is not None:
            if retry_source is not None:
                feedback = build_prior_failures_snapshot(
                    retry_source,
                    repo.root,
                    include_prompt=project_config.context.include_prior_artifacts != "none",
                )
            else:
                include_prior = project_config.context.include_prior_artifacts != "none"
                feedback = {
                    "source_run_id": retry_of,
                    "source_artifacts": [],
                    "validation_artifacts": [],
                    "validation_signatures": [],
                    "validation": [],
                    "remediations": [],
                    "latest_review": None,
                    "attempted_repair_count": 0,
                    "evidence_unavailable": True,
                    "prompt_excluded": not include_prior,
                    "omission_notice": False,
                    "prompt_text": (
                        f"Historical evidence from source run {retry_of}. "
                        "Evidence unavailable: legacy source record was not supplied."
                        if include_prior
                        else "Prompt history excluded by context.include_prior_artifacts=none; "
                        f"source run {retry_of} remains linked for operator diagnostics."
                    ),
                }
            store.write_json_artifact(PRIOR_FAILURES_ARTIFACT, feedback, immutable=True)
            if feedback["evidence_unavailable"]:
                store.append_event({"event": "prior_failures_unavailable", "source_run": retry_of})
            if feedback["prompt_excluded"]:
                store.append_event({"event": "prior_failures_excluded", "source_run": retry_of})
        store.write_text_artifact("request.md", request_text + "\n", immutable=True)
        store.write_json_artifact("acceptance-criteria.json", acceptance_criteria, immutable=True)
        if external_plan is not None:
            stored_external = "planning/external-plan-pending.md"
            store.write_bytes_artifact(stored_external, external_plan.read_bytes(), immutable=True)
            store.update_manifest(external_plan_artifact=stored_external)
            external_plan = store.root / stored_external
        classifier_references = classification_references_for_run(
            store,
            project_config,
            request_text,
            fence,
            persist_if_missing=True,
        )
    except BaseException as exc:
        with suppress(BaseException):
            _terminal(
                engine,
                RunState.FAILED,
                "run setup failed",
                "environment" if isinstance(exc, OSError) else "other",
                failure_signature="setup:before-classification",
                stop_code="environment" if isinstance(exc, OSError) else "other",
            )
        try:
            with suppress(BaseException):
                cleanup_worktree_if_unproductive(engine, repo, worktree)
        finally:
            with suppress(BaseException):
                claim.__exit__(None, None, None)
        raise

    try:
        classifier_artifacts = ["request.md", "acceptance-criteria.json", "roles.json"]
        if continue_patch is not None:
            classifier_artifacts.append(CONTINUED_CHANGE_ARTIFACT)
        receipt("classifier", classifier_artifacts)
        classification = classify(
            registry,
            request_text,
            project_config.project.project_class,
            store,
            project_config.profiles.available,
            classifier_references,
            acceptance_criteria,
        )
        specification_minimum, specification_reason = specification_minimum_tier(request_text)
        if specification_minimum is not None and _tier_rank(classification.tier) < _tier_rank(
            specification_minimum
        ):
            classification = classification.model_copy(
                update={
                    "tier": specification_minimum,
                    "rationale": f"{classification.rationale}; raised to standard because {specification_reason}",
                }
            )
            store.write_json_artifact("classification.json", classification.model_dump(mode="json"))
            store.append_event(
                {
                    "event": "classification_tier_raised",
                    "tier": str(specification_minimum),
                    "reason": specification_reason,
                }
            )
        engine.transition(RunState.CLASSIFIED)
        runtime = resolve_runtime_context(repo.root, project_config, classification.profiles, fence)
        dependency_report = ""
        if project_config.context.dependency_discovery:
            inventory, source_metadata = dependency_sources(repo.root, project_config)
            dependency_report = discover_dependencies(
                registry, request_text, inventory, source_metadata, runtime.invariants, store
            )
            store.append_event(
                {
                    "event": "dependency_discovery_completed",
                    "source_count": len(source_metadata["sources"]),
                    "budget_exhausted": source_metadata["budget_exhausted"],
                }
            )
        config_layers = [
            project_config.model_dump(mode="json"),
            *[
                {"profiles": {name: definition.model_dump(mode="json")}}
                for name, definition in project_config.profiles.definitions.items()
                if name in classification.profiles
            ],
        ]
        if tier_override:
            config_layers.append({"run": {"tier_override": str(tier_override)}})
        resolved_policy = resolve_run_policy(config_layers)
        for key in PROTECTED_DEFAULTS:
            if resolved_policy.get(key) is not False:
                raise ContextContractError(f"protected policy {key} did not resolve to false")
        store.update_manifest(
            available_profiles=project_config.profiles.available,
            config_layers=config_layers,
            resolved_config=resolved_policy,
        )
        store.update_manifest(
            active_profiles=classification.profiles,
            minimum_tier=runtime.minimum_tier,
            context_included=runtime.included,
            context_excluded=runtime.excluded,
            context_conflicts=runtime.conflicts,
        )
        store.write_json_artifact(
            "context.json",
            {
                "included": runtime.included,
                "excluded": runtime.excluded,
                "conflicts": runtime.conflicts,
                "available_profiles": project_config.profiles.available,
                "active_profiles": classification.profiles,
                "scope_fence": {
                    "include": project_config.scope.include,
                    "exclude": project_config.scope.exclude,
                },
            },
        )
        effective_tier = _effective_tier(
            classification.tier, tier_override, runtime.minimum_tier, downgrade_reason
        )
        store.update_manifest(tier=effective_tier)
        store.update_manifest(
            validation_input_hashes=validation_file_hashes(repo, project_config.validation_files)
        )
        stages_for(effective_tier)
        if external_plan is not None and effective_tier not in {Tier.STANDARD, Tier.SUBSTANTIAL}:
            raise ContextContractError("an external plan requires the standard or substantial tier")
        if effective_tier is not classification.tier:
            downgraded = _tier_rank(effective_tier) < _tier_rank(classification.tier)
            store.append_event(
                {
                    "event": "tier_resolved",
                    "classified": str(classification.tier),
                    "effective": str(effective_tier),
                    "override": str(tier_override) if tier_override else None,
                    "minimum": str(runtime.minimum_tier) if runtime.minimum_tier else None,
                    "downgraded": downgraded,
                    "downgrade_reason": downgrade_reason if downgraded else None,
                }
            )

        protected = ContextRef(
            label=Category.INVARIANTS,
            path="framework:protected",
            content=json.dumps(store.read_manifest().resolved_config, sort_keys=True),
        )
        invariants = [*runtime.invariants, protected]
        scope_context = ContextRef(
            label=Category.SCOPE_FENCE,
            path="config:scope",
            content=json.dumps(
                {
                    "include": project_config.scope.include,
                    "exclude": project_config.scope.exclude,
                },
                sort_keys=True,
            ),
        )
        stage_artifacts = [
            "classification.json",
            "context.json",
            "request.md",
            "acceptance-criteria.json",
            "roles.json",
        ]
        if dependency_report:
            stage_artifacts.append("dependency-report.json")
        approved: str
        approval_artifact: str
        reused_review_artifact: str | None = None
        if effective_tier in {Tier.STANDARD, Tier.SUBSTANTIAL}:
            receipt("planner", stage_artifacts)
            reuse_receipt = None
            reuse_matches = False
            reuse_reason = ""
            if reuse_source is not None and external_plan is not None:
                candidate_bytes = external_plan.read_bytes()
                reuse_matches, reuse_reason, reuse_receipt = match_plan_review_source(
                    reuse_source,
                    current=store,
                    plan_bytes=candidate_bytes,
                    profile_constraints=runtime.profile_constraints,
                    dependency_report=_dependency_review_identity(store),
                )
                if not reuse_matches:
                    store.append_event(
                        {
                            "event": "plan_review_reuse_refused",
                            "source_run": reuse_source.run_id,
                            "reason": reuse_reason,
                        }
                    )
            if reuse_matches and external_plan is not None:
                version, _ = store.import_external_plan(external_plan)
                assert reuse_receipt is not None
                source_review = reuse_source.root / reuse_receipt["final_review"]["path"]
                copied_review = "review/plan-review-v1.json"
                reused_review_artifact = copied_review
                store.write_bytes_artifact(
                    copied_review, source_review.read_bytes(), immutable=True
                )
                store.record_finding_index(source_finding_index)
                store.update_manifest(
                    plan_review_provenance={
                        "source_run_id": reuse_source.run_id,
                        "source_receipt": reuse_receipt,
                    }
                )
                store.append_event(
                    {
                        "event": "plan_review_reused",
                        "source_run": reuse_source.run_id,
                        "receipt": PLAN_REVIEW_RECEIPT,
                    }
                )
                engine.transition(RunState.PLANNED, role="orchestrator")
                engine.transition(RunState.PLAN_REVIEWED, role="orchestrator")
                store.approve_plan(version)
                create_plan_review_receipt(
                    store,
                    version,
                    store.root / copied_review,
                    runtime.profile_constraints,
                    _dependency_review_identity(store),
                )
                engine.transition(RunState.PLAN_FINALIZED)
                receipt(
                    "implementation_worker",
                    [
                        f"planning/{version.name}",
                        "planning/approved-plan.md",
                        PLAN_REVIEW_RECEIPT,
                        copied_review,
                        "review/finding-index.json",
                    ],
                )
                approved = store.approved_plan()
                approval_artifact = "planning/approved-plan.md"
                plan_review = None
            else:
                if reuse_source is not None:
                    store.append_event(
                        {
                            "event": "plan_review_reuse_reason",
                            "source_run": reuse_source.run_id,
                            "reason": reuse_reason
                            or "source plan bytes unavailable; review will run",
                        }
                    )
            if external_plan is not None:
                if not reuse_matches:
                    version, digest = store.import_external_plan(external_plan)
                    store.append_event(
                        {
                            "event": "external_plan_selected",
                            "version": version.name,
                            "sha256": digest,
                        }
                    )
            else:
                if not reuse_matches:
                    version = plan(
                        registry,
                        request_text,
                        invariants,
                        runtime.references,
                        store,
                        scope_fence=scope_context,
                        acceptance_criteria=acceptance_criteria,
                        dependency_report=dependency_report,
                    )
            if not reuse_matches:
                engine.transition(
                    RunState.PLANNED, role="orchestrator" if external_plan is not None else None
                )
                receipt("plan_reviewer", [f"planning/{version.name}"])
                plan_text = version.read_text(encoding="utf-8")
                plan_review = review_plan(
                    registry,
                    request_text,
                    plan_text,
                    runtime.profile_constraints,
                    store,
                    acceptance_criteria,
                    scope_context,
                    dependency_report,
                )
                engine.transition(RunState.PLAN_REVIEWED)
                receipt(
                    "plan_reconciler" if plan_review.blocking_ids() else "plan_finalization",
                    [
                        f"review/{max((store.root / 'review').glob('plan-review-v*.json')).name}",
                        "review/finding-index.json",
                    ],
                )
                reconciliation_cycles = 0
            while not reuse_matches and plan_review.blocking_ids():
                if plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                    target = (
                        RunState.BLOCKED
                        if plan_review.outcome is Outcome.BLOCKED
                        else RunState.ESCALATED
                    )
                    _terminal(
                        engine,
                        target,
                        "plan review: " + str(plan_review.outcome),
                        "plan_review_escalated"
                        if plan_review.outcome is Outcome.ESCALATE
                        else "other",
                        stop_code="plan_review_blocked" if target is RunState.BLOCKED else None,
                    )
                    return _outcome(engine, repo=repo, worktree=worktree)
                reconciliation_cycles += 1
                if reconciliation_cycles > MAX_REMEDIATION_CYCLES:
                    raise RemediationExhausted(
                        f"plan findings {[f.id for f in plan_review.findings]} survived "
                        f"{MAX_REMEDIATION_CYCLES} reconciliation cycles",
                        cause="plan_review_exhausted",
                    )
                engine.transition(RunState.PLANNED, role="plan_reconciler")
                version = reconcile(
                    registry,
                    plan_text,
                    plan_review.findings,
                    store,
                    request_text,
                    acceptance_criteria,
                    scope_context,
                    dependency_report,
                    cycle=reconciliation_cycles,
                )
                receipt("plan_reviewer", [f"planning/{version.name}"], reconciliation_cycles)
                plan_text = version.read_text(encoding="utf-8")
                plan_review = review_plan(
                    registry,
                    request_text,
                    plan_text,
                    runtime.profile_constraints,
                    store,
                    acceptance_criteria,
                    scope_context,
                    dependency_report,
                )
                engine.transition(RunState.PLAN_REVIEWED)
                receipt(
                    "plan_reconciler" if plan_review.blocking_ids() else "plan_finalization",
                    [
                        f"review/{max((store.root / 'review').glob('plan-review-v*.json')).name}",
                        "review/finding-index.json",
                    ],
                    reconciliation_cycles,
                )
            if not reuse_matches and plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                target = (
                    RunState.BLOCKED
                    if plan_review.outcome is Outcome.BLOCKED
                    else RunState.ESCALATED
                )
                _terminal(
                    engine,
                    target,
                    "plan review: " + str(plan_review.outcome),
                    "plan_review_escalated" if plan_review.outcome is Outcome.ESCALATE else "other",
                    stop_code="plan_review_blocked" if target is RunState.BLOCKED else None,
                )
                return _outcome(engine, repo=repo, worktree=worktree)
            if not reuse_matches:
                store.approve_plan(version)
                final_review = max(
                    (store.root / "review").glob("plan-review-v*.json"),
                    key=lambda path: int(path.stem.rsplit("-v", 1)[1]),
                )
                create_plan_review_receipt(
                    store,
                    version,
                    final_review,
                    runtime.profile_constraints,
                    _dependency_review_identity(store),
                )
                engine.transition(RunState.PLAN_FINALIZED)
                receipt(
                    "implementation_worker",
                    ["planning/approved-plan.md", PLAN_REVIEW_RECEIPT],
                )
                approved = store.approved_plan()
                approval_artifact = "planning/approved-plan.md"
        else:
            receipt("classifier", stage_artifacts)
            approved = _task_contract(request_text, project_config, acceptance_criteria, invariants)
            _persist_task_contract(store, approved)
            approval_artifact = "planning/task-contract.md"
            receipt(
                "implementation_worker",
                [*stage_artifacts, approval_artifact],
            )

        save_contract(store, approved, project_config, effective_tier, acceptance_criteria)
        contract = load_contract(store, approved)
        approval_required = _approval_required(
            project_config, effective_tier, classification.profiles
        )
        store.update_manifest(approval_required=approval_required)
        if approval_required:
            store.write_text_artifact(
                "approval/human-approval.md",
                _approval_summary(
                    request_text,
                    project_config,
                    effective_tier,
                    classification.profiles,
                    approved,
                ),
                immutable=True,
            )
            receipt(
                "implementation_worker",
                [
                    approval_artifact,
                    "approval/human-approval.md",
                    *(
                        [PLAN_REVIEW_RECEIPT]
                        if effective_tier in {Tier.STANDARD, Tier.SUBSTANTIAL}
                        else []
                    ),
                    *([reused_review_artifact] if reused_review_artifact else []),
                    *([CONTRACT_ARTIFACT] if contract is not None else []),
                ],
            )
            store.append_event(
                {
                    "event": "approval_required",
                    "tier": str(effective_tier),
                    "artifact": "approval/human-approval.md",
                }
            )
            engine.transition(RunState.AWAITING_APPROVAL)
            return _outcome(
                engine,
                reason="human approval required before execution",
                repo=repo,
                worktree=worktree,
            )

        receipt(
            "implementation_worker",
            [
                approval_artifact,
                *(
                    [PLAN_REVIEW_RECEIPT]
                    if effective_tier in {Tier.STANDARD, Tier.SUBSTANTIAL}
                    else []
                ),
                *([reused_review_artifact] if reused_review_artifact else []),
                *([CONTRACT_ARTIFACT] if contract is not None else []),
            ],
        )
        seed_patch_applied = apply_seed_patch_once(store, worktree_repo, base_commit, fence)
        if seed_patch_applied:
            receipt(
                "implementation_worker",
                [approval_artifact, SEED_PATCH_ARTIFACT, SEED_PATCH_RECEIPT],
            )

        engine.transition(RunState.EXECUTING)
        if continue_patch is not None:
            try:
                applied = apply_continued_change(store, worktree_repo, base_commit, fence)
                if not applied:
                    raise CheckpointError("continued change patch is missing")
            except GitCommandError as exc:
                _terminal(
                    engine,
                    RunState.ESCALATED,
                    f"the saved change from {retry_of} no longer applies to the current "
                    f"repository ({exc}); use `dev-orch retry --fresh` to re-implement",
                )
                return _outcome(engine, repo=repo, worktree=worktree)
            store.append_event({"event": "change_continued", "source_run": retry_of})
            receipt("validation", [CONTINUED_CHANGE_ARTIFACT])
        else:
            before_worker = _deliverable_patch(worktree_repo, base_commit)
            _run_worktree_stage(
                worktree_repo,
                base_commit,
                fence,
                links=worker_links,
                action=lambda: execute(
                    registry,
                    approved,
                    invariants,
                    runtime.references,
                    worktree,
                    store,
                    runtime.profile_constraints,
                    scope_context,
                    dependency_report,
                    contract,
                ),
            )
            after_worker = _deliverable_patch(worktree_repo, base_commit)
            if not after_worker:
                _record_no_progress(
                    engine,
                    cause="worker_no_changes",
                    stage="implementation_worker",
                    before=before_worker,
                    after=after_worker,
                )
                return _outcome(engine, repo=repo, worktree=worktree)
            receipt("validation", ["execution/worker-result.json"])

        def review_repair(review, round_number):
            """Describe the next remediation round while the review that caused it is fresh."""
            scope = review_repair_scope(review, round_number, fence)
            return (None, []) if scope is None else (scope, [saved(scope)[1]])

        cycle = 1
        while True:
            engine.transition(RunState.VALIDATING)
            outcomes = validate()
            review_next = (
                "implementation_reviewer"
                if _implementation_review_required(effective_tier, project_config.validation)
                else "verifier"
            )
            _save_validation(store, outcomes, cycle - 1)
            outcomes, repair_failed = repair_failed_validation(outcomes, cycle - 1, review_next)
            if repair_failed:
                _terminal(
                    engine,
                    RunState.FAILED,
                    "required validation failed after repair",
                    "validation_failed",
                    failure_signature="validation:"
                    + ",".join(outcome.name for outcome in outcomes if not outcome.passed),
                )
                return _outcome(engine, repo=repo, worktree=worktree)

            diff = worktree_repo.change_diff(base_commit)
            review_inventory = worktree_repo.change_inventory(base_commit)
            implementation_review = None
            if _implementation_review_required(effective_tier, project_config.validation):
                engine.transition(RunState.IMPLEMENTATION_REVIEW)
                implementation_review = review_implementation(
                    registry,
                    approved,
                    diff,
                    _evidence(outcomes),
                    runtime.profile_constraints,
                    store,
                    worktree,
                    dependency_report,
                )
                review_scope, review_scope_artifacts = review_repair(implementation_review, cycle)
                receipt(
                    "remediation"
                    if review_repair_findings(implementation_review, cycle)
                    else "verifier",
                    [
                        f"review/{max((store.root / 'review').glob('implementation-review-v*.json')).name}",
                        "review/finding-index.json",
                        *review_scope_artifacts,
                    ],
                    cycle - 1,
                )
                cleanup_ephemeral_paths(worktree_repo.root)
                if worktree_repo.change_inventory(base_commit) != review_inventory:
                    raise ContextContractError(
                        "read-only implementation review changed the worktree"
                    )
            else:
                store.append_event(
                    {
                        "event": "implementation_review_skipped",
                        "tier": str(effective_tier),
                        "reason": "tier stage path omits implementation review",
                    }
                )
            while implementation_review is not None and review_repair_findings(
                implementation_review, cycle
            ):
                if implementation_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                    target = (
                        RunState.BLOCKED
                        if implementation_review.outcome is Outcome.BLOCKED
                        else RunState.ESCALATED
                    )
                    _terminal(
                        engine,
                        target,
                        "implementation review: " + str(implementation_review.outcome),
                        "implementation_review_escalated"
                        if implementation_review.outcome is Outcome.ESCALATE
                        else "other",
                        stop_code="implementation_review_blocked"
                        if target is RunState.BLOCKED
                        else None,
                    )
                    return _outcome(engine, repo=repo, worktree=worktree)
                engine.transition(RunState.REMEDIATION)
                blocking = review_repair_findings(implementation_review, cycle)
                current_state = diff or "No committed or working-tree diff was observed."
                before_remediation = _deliverable_patch(worktree_repo, base_commit)
                guarded_repair(
                    review_scope,
                    lambda blocking=blocking, current_state=current_state, cycle=cycle, review_scope=review_scope: (
                        remediate(
                            registry,
                            blocking,
                            approved,
                            current_state,
                            worktree,
                            store,
                            cycle=cycle,
                            dependency_report=dependency_report,
                            contract=contract,
                            repair=review_scope,
                        )
                    ),
                )
                after_remediation = _deliverable_patch(worktree_repo, base_commit)
                if after_remediation == before_remediation:
                    _record_no_progress(
                        engine,
                        cause="remediation_no_progress",
                        stage="implementation_remediation",
                        before=before_remediation,
                        after=after_remediation,
                        cycle=cycle,
                    )
                    return _outcome(engine, repo=repo, worktree=worktree)
                receipt("validation", [f"execution/remediation-v{cycle}.json"], cycle)
                engine.transition(RunState.VALIDATING)
                outcomes = validate()
                _save_validation(store, outcomes, cycle)
                outcomes, repair_failed = repair_failed_validation(
                    outcomes, cycle, "implementation_reviewer"
                )
                if repair_failed:
                    _terminal(
                        engine,
                        RunState.FAILED,
                        "required validation failed after repair",
                        "validation_failed",
                        failure_signature="validation:"
                        + ",".join(outcome.name for outcome in outcomes if not outcome.passed),
                    )
                    return _outcome(engine, repo=repo, worktree=worktree)
                diff = worktree_repo.change_diff(base_commit)
                review_inventory = worktree_repo.change_inventory(base_commit)
                engine.transition(RunState.IMPLEMENTATION_REVIEW)
                implementation_review = review_implementation(
                    registry,
                    approved,
                    diff,
                    _evidence(outcomes),
                    runtime.profile_constraints,
                    store,
                    worktree,
                    dependency_report,
                )
                review_scope, review_scope_artifacts = review_repair(
                    implementation_review, cycle + 1
                )
                receipt(
                    "remediation"
                    if review_repair_findings(implementation_review, cycle + 1)
                    else "verifier",
                    [
                        f"review/{max((store.root / 'review').glob('implementation-review-v*.json')).name}",
                        "review/finding-index.json",
                        *review_scope_artifacts,
                    ],
                    cycle,
                )
                cleanup_ephemeral_paths(worktree_repo.root)
                if worktree_repo.change_inventory(base_commit) != review_inventory:
                    raise ContextContractError(
                        "read-only implementation review changed the worktree"
                    )
                cycle += 1

            if implementation_review is not None and implementation_review.outcome in {
                Outcome.BLOCKED,
                Outcome.ESCALATE,
            }:
                target = (
                    RunState.BLOCKED
                    if implementation_review.outcome is Outcome.BLOCKED
                    else RunState.ESCALATED
                )
                _terminal(
                    engine,
                    target,
                    "implementation review: " + str(implementation_review.outcome),
                    "implementation_review_escalated"
                    if implementation_review.outcome is Outcome.ESCALATE
                    else "other",
                    stop_code="implementation_review_blocked"
                    if target is RunState.BLOCKED
                    else None,
                )
                return _outcome(engine, repo=repo, worktree=worktree)

            engine.transition(RunState.FINAL_VERIFICATION)
            review_findings = (
                implementation_review.findings if implementation_review is not None else []
            )
            previous = None
            while True:
                command_evidence = _criteria_command_evidence(
                    repo, worktree_repo, base_commit, project_config, acceptance_criteria
                )
                verification = verify(
                    registry,
                    request_text,
                    approved,
                    acceptance_criteria,
                    diff,
                    _evidence(outcomes) + command_evidence,
                    review_findings,
                    store,
                    worktree,
                    dependency_report,
                    previous,
                )
                receipt("commit", ["verification/final-verification.json"], cycle - 1)
                cleanup_ephemeral_paths(worktree_repo.root)
                if worktree_repo.change_inventory(base_commit) != review_inventory:
                    raise ContextContractError("read-only verification changed the worktree")
                accepted = verification_allows_completion(
                    verification, acceptance_criteria, review_findings
                )
                route = verification_route(verification, review_findings)
                current = store.read_manifest()
                if accepted or route != "ask" or not verification_ask_available(current):
                    break
                # The verifier wants only evidence the orchestrator supplies: ask once more.
                store.update_manifest(
                    verification_ask_rounds=[
                        *current.verification_ask_rounds,
                        current.verification_repairs_reserved,
                    ]
                )
                receipt("verifier", ["verification/final-verification.json"], cycle - 1)
                previous = verification
            if accepted:
                break
            evidence_only = (
                all(outcome.passed for outcome in outcomes)
                and verification.criteria == acceptance_criteria
                and not any(verdict.verdict == "FAIL" for verdict in verification.verdicts)
                and not any(
                    finding.severity.value == "blocking"
                    for finding in (
                        implementation_review.findings if implementation_review is not None else []
                    )
                )
            )
            rejection_class = "evidence" if evidence_only else "product"
            if (
                verification.criteria != acceptance_criteria
                or [verdict.criterion for verdict in verification.verdicts] != acceptance_criteria
                or (
                    all(verdict.verdict == "PASS" for verdict in verification.verdicts)
                    and route is None
                )
            ):
                # Either the verifier did not judge the supplied criteria, or it failed none of
                # them and named no review finding a scoped repair could address.
                _terminal(
                    engine,
                    RunState.FAILED,
                    "final verification did not provide an unqualified PASS for the supplied "
                    "criteria",
                    failure_class=rejection_class,
                    failure_signature="verification:acceptance-criteria",
                    stop_code="verification_rejected",
                )
                return _outcome(engine, verification, repo=repo, worktree=worktree)
            if route in (None, "ask"):
                _terminal(
                    engine,
                    RunState.ESCALATED,
                    "final verification: " + str(verification.outcome),
                    "verification_escalated",
                    failure_class=rejection_class,
                    failure_signature="verification:acceptance-criteria",
                )
                return _outcome(engine, verification, repo=repo, worktree=worktree)
            reserved = store.read_manifest().verification_repairs_reserved
            if reserved >= MAX_VERIFICATION_REPAIRS:
                _terminal(
                    engine,
                    RunState.ESCALATED,
                    f"final verification still rejected the change after {reserved} repair rounds",
                    "verification_exhausted",
                    failure_class=rejection_class,
                    failure_signature="verification:acceptance-criteria",
                )
                return _outcome(engine, verification, repo=repo, worktree=worktree)
            round_number = reserved + 1
            repair_info, repair_artifact = saved(
                verification_repair_scope(verification, round_number, fence, review_findings)
            )
            store.update_manifest(verification_repairs_reserved=round_number)
            receipt(
                "verification_repair",
                ["verification/final-verification.json", repair_artifact],
                cycle - 1,
            )
            engine.transition(RunState.REMEDIATION)
            before_repair = _deliverable_patch(worktree_repo, base_commit)
            before_files = file_digests(worktree, worktree_repo.change_inventory(base_commit))
            guarded_repair(
                repair_info,
                lambda verification=verification, diff=diff, outcomes=outcomes, repair_info=repair_info, round_number=round_number: (
                    repair_verification(
                        registry,
                        approved,
                        verification,
                        diff,
                        _evidence(outcomes),
                        worktree,
                        store,
                        dependency_report,
                        contract,
                        repair_info,
                        round_number,
                    )
                ),
            )
            after_repair = _deliverable_patch(worktree_repo, base_commit)
            missing_test = repair_info.get("mode") == "evidence" and not any(
                is_test_path(path)
                for path in edited_paths(
                    worktree, before_files, worktree_repo.change_inventory(base_commit)
                )
            )
            if after_repair == before_repair or missing_test:
                _record_no_progress(
                    engine,
                    cause="remediation_no_progress",
                    stage="verification_repair",
                    before=before_repair,
                    after=after_repair,
                    cycle=round_number,
                    missing_test=missing_test,
                )
                return _outcome(engine, repo=repo, worktree=worktree)
            receipt(
                "validation", [f"execution/verification-repair-v{round_number}.json"], cycle - 1
            )

        enforce_fence(worktree_repo, base_commit, fence)
        changed = worktree_repo.change_inventory(base_commit)
        if not changed:
            _terminal(
                engine,
                RunState.FAILED,
                "the run produced no change to commit",
                stop_code="worker_no_changes",
            )
            return _outcome(engine, verification, repo=repo, worktree=worktree)
        final_commit = worktree_repo.commit_snapshot(
            f"ai({store.run_id}): complete orchestrated run", changed
        )
        worktree_repo.ensure_clean()
        if final_commit == base_commit:
            raise ContextContractError("final commit does not advance from the recorded base")
        if not worktree_repo.is_ancestor(base_commit, final_commit):
            raise ContextContractError("final commit does not descend from the recorded base")
        store.update_manifest(
            git=GitBlock(
                base_commit=base_commit,
                branch=store.read_manifest().git.branch,
                worktree=str(worktree),
                final_commit=final_commit,
            )
        )
        store.write_json_artifact(
            "execution/execution-summary.json",
            {"changed_files": changed, "base_commit": base_commit, "final_commit": final_commit},
        )
        engine.transition(RunState.COMPLETE_LOCAL)
        return _outcome(engine, verification, repo=repo, worktree=worktree)

    except (
        RemediationExhausted,
        ScopeViolation,
        EmptySnapshotError,
        FindingIdentityAmbiguity,
        SchemaEscalation,
        AgentInvocationError,
        ContextContractError,
        ReadOnlyRoleUnsupportedError,
        NoSafeRepairScope,
    ) as exc:
        if isinstance(exc, AgentInvocationError):
            _terminal(
                engine,
                RunState.ESCALATED,
                str(exc),
                "environment",
                failure_class="environment",
            )
            return _outcome(engine, repo=repo, worktree=worktree)
        cause = (
            exc.cause
            if isinstance(exc, RemediationExhausted)
            else "scope_violation"
            if isinstance(exc, ScopeViolation)
            else "repair_scope_unsafe"
            if isinstance(exc, NoSafeRepairScope)
            else "other"
        )
        stop_code = None
        if isinstance(exc, PromptBudgetError):
            stop_code = "prompt_budget"
        elif isinstance(exc, SchemaEscalation):
            stop_code = "schema_rejected"
        _terminal(engine, RunState.ESCALATED, str(exc), cause, stop_code=stop_code)
        return _outcome(engine, repo=repo, worktree=worktree)
    except (IllegalTransitionError, UnsupportedTierError, TierDowngradeError) as exc:
        _terminal(engine, RunState.ESCALATED, str(exc))
        return _outcome(engine, repo=repo, worktree=worktree)
    except (UsageLimitError, AgentTimeoutError, AgentInterruptedError) as exc:
        try:
            enforce_fence(worktree_repo, base_commit, fence)
            snapshot = capture_snapshot(worktree_repo, base_commit)
        except (ScopeViolation, OSError, GitCommandError) as guard_error:
            _terminal(engine, RunState.ESCALATED, str(guard_error))
            return _outcome(engine, repo=repo, worktree=worktree)
        if isinstance(exc, UsageLimitError):
            observation = exc.observation
            due = next_attempt(observation)
            pause_artifact = (
                f"execution/pause-snapshot-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.json"
            )
            store.write_json_artifact(pause_artifact, snapshot, immutable=True)
            store.update_manifest(
                pause=PauseRecord(
                    provider=observation.provider,
                    role=exc.role,
                    limit_kind=observation.limit_kind,
                    observed_at=observation.observed_at,
                    reset_at=observation.reset_at,
                    next_attempt_at=due,
                    diagnostic=observation.diagnostic,
                    worktree_digest=snapshot["digest"],
                    snapshot_artifact=pause_artifact,
                    account_id=observation.account_id,
                    raw_diagnostic_ref=observation.raw_diagnostic_ref,
                ),
            )
            engine.transition(RunState.PAUSED_USAGE)
            if auto_resume and due is not None:
                try:
                    (scheduler_queue or SchedulerQueue()).register(repo.root, store.run_id, due)
                except OSError as schedule_error:
                    store.append_event(
                        {"event": "scheduler_register_failed", "detail": str(schedule_error)}
                    )
        else:
            pause_artifact = (
                f"execution/pause-snapshot-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.json"
            )
            store.write_json_artifact(pause_artifact, snapshot, immutable=True)
            store.update_manifest(
                pause=PauseRecord(
                    provider=exc.provider,
                    role=exc.role,
                    observed_at=datetime.now(UTC),
                    worktree_digest=snapshot["digest"],
                    snapshot_artifact=pause_artifact,
                    diagnostic=str(exc),
                ),
            )
            engine.transition(RunState.PAUSED_INTERRUPTED)
        return _outcome(engine, repo=repo, worktree=worktree)
    except ValidationEnvironmentError as exc:
        _terminal(engine, RunState.FAILED, str(exc), "environment")
        return _outcome(engine, repo=repo, worktree=worktree)
    except (OSError, subprocess.SubprocessError) as exc:
        _terminal(
            engine,
            RunState.ESCALATED,
            f"provider invocation failed: {exc}",
            "environment",
            failure_class="environment",
            failure_signature="provider-invocation:"
            + ",".join(
                sorted({binding.adapter for binding in store.read_manifest().roles.values()})
            ),
        )
        return _outcome(engine, repo=repo, worktree=worktree)
    except Exception as exc:  # noqa: BLE001 - guarantee a durable terminal state
        _terminal(engine, RunState.FAILED, f"unexpected workflow failure: {exc}")
        return _outcome(engine, repo=repo, worktree=worktree)
    finally:
        claim.__exit__(None, None, None)


def _normalize_criteria(criteria: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        value = " ".join(criterion.split())
        if not value:
            raise ContextContractError(
                "acceptance criteria must be non-empty after trimming whitespace"
            )
        key = value.casefold()
        if key in seen:
            raise ContextContractError(
                f"acceptance criteria must be unique after normalization: {value!r}"
            )
        seen.add(key)
        normalized.append(value)
    if not normalized:
        raise ContextContractError(
            "a run requires at least one acceptance criterion; "
            "there is nothing for final verification to judge"
        )
    return normalized
