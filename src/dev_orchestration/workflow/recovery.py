"""Continue a saved run from its last completed stage."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import AgentInterruptedError, AgentTimeoutError
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.adapters.usage import UsageAwareAdapter, UsageLimitError, next_attempt
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.context.assembler import Category, ContextContractError, PromptBudgetError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, RunState, Tier
from dev_orchestration.domain.findings import (
    ReviewResult,
    Verification,
    verification_allows_completion,
)
from dev_orchestration.domain.run import GitBlock, PauseRecord
from dev_orchestration.git.repo import GitCommandError, GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    classification_references_for_run,
    resolve_run_policy,
    resolve_runtime_context,
)
from dev_orchestration.workflow.checkpoints import (
    dependency_discovery_default_omission,
    input_hashes,
    project_config_matches,
    record_stage,
    validation_files_default_omission,
)
from dev_orchestration.workflow.dependencies import dependency_sources
from dev_orchestration.workflow.engine import Engine
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
from dev_orchestration.workflow.legacy import recover_legacy
from dev_orchestration.workflow.plan_reuse import (
    RECEIPT as PLAN_REVIEW_RECEIPT,
)
from dev_orchestration.workflow.plan_reuse import (
    create_receipt as create_plan_review_receipt,
)
from dev_orchestration.workflow.retry_feedback import context_ref as prior_failures_context
from dev_orchestration.workflow.runner import (
    CONTINUED_CHANGE_ARTIFACT,
    RunOutcome,
    _approval_required,
    _approval_summary,
    _deliverable_patch,
    _dependency_review_identity,
    _effective_tier,
    _evidence,
    _implementation_review_required,
    _outcome,
    _persist_task_contract,
    _record_no_progress,
    _run_validation_stage,
    _run_worktree_stage,
    _save_validation,
    _task_contract,
    _terminal,
    _tier_rank,
    apply_continued_change,
)
from dev_orchestration.workflow.scheduler import SchedulerQueue
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    cleanup_ephemeral_paths,
    enforce_fence,
)
from dev_orchestration.workflow.seed_patch import (
    PATCH_ARTIFACT as SEED_PATCH_ARTIFACT,
)
from dev_orchestration.workflow.seed_patch import (
    RECEIPT_ARTIFACT as SEED_PATCH_RECEIPT,
)
from dev_orchestration.workflow.seed_patch import (
    apply_seed_patch_once,
)
from dev_orchestration.workflow.snapshot import capture_snapshot, require_snapshot
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    RemediationExhausted,
    classify,
    discover_dependencies,
    execute,
    plan,
    reconcile,
    remediate,
    repair_validation,
    review_implementation,
    review_plan,
    verify,
)
from dev_orchestration.workflow.tiers import specification_minimum_tier
from dev_orchestration.workflow.validation import (
    ValidationEnvironmentError,
    ValidationOutcome,
    all_passed,
    run_validations,
    validation_environment,
    validation_file_hashes,
)


class ResumeRefused(RuntimeError):
    """Saved state cannot safely continue."""


_STAGE_STATE = {
    "classifier": RunState.CREATED,
    "planner": RunState.CLASSIFIED,
    "plan_reviewer": RunState.PLANNED,
    "plan_reconciler": RunState.PLAN_REVIEWED,
    "plan_finalization": RunState.PLAN_REVIEWED,
    "implementation_worker": RunState.EXECUTING,
    "validation": RunState.VALIDATING,
    "validation_repair": RunState.REMEDIATION,
    "implementation_reviewer": RunState.IMPLEMENTATION_REVIEW,
    "remediation": RunState.REMEDIATION,
    "verifier": RunState.FINAL_VERIFICATION,
    "commit": RunState.FINAL_VERIFICATION,
}


def _latest(store: RunStore, folder: str, prefix: str) -> Path:
    files = list((store.root / folder).glob(f"{prefix}-v*.json"))
    if not files:
        raise ResumeRefused(f"saved {prefix} artifact is missing")
    return max(files, key=lambda item: int(item.stem.rsplit("-v", 1)[1]))


def _latest_plan(store: RunStore) -> Path:
    versions = store.plan_versions()
    if not versions:
        raise ResumeRefused("saved plan version is missing")
    return versions[-1]


def _approved(store: RunStore) -> str:
    manifest = store.read_manifest()
    is_contract = manifest.plan_origin == "task_contract"
    path = store.root / (
        "planning/task-contract.md" if is_contract else "planning/approved-plan.md"
    )
    if not path.is_file():
        raise ResumeRefused(
            "saved trivial task contract is missing; start a new run"
            if is_contract
            else "approved plan is missing; start a new run because this run has no recoverable plan"
        )
    content = path.read_text(encoding="utf-8")
    if (
        manifest.plan_sha256
        and hashlib.sha256(path.read_bytes()).hexdigest() != manifest.plan_sha256
    ):
        raise ResumeRefused("approved plan hash changed")
    return content


def _check_run(
    store: RunStore,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    worktree: Path,
    *,
    automatic: bool,
    adopt_worktree: bool,
    now: datetime,
    for_approval: bool = False,
) -> dict:
    manifest = store.read_manifest()
    if manifest.status in {
        RunState.COMPLETE_LOCAL,
        RunState.CANCELLED,
        RunState.BLOCKED,
        RunState.ESCALATED,
        RunState.FAILED,
    }:
        raise ResumeRefused(f"run is {manifest.status}; it cannot resume")
    if manifest.status is RunState.AWAITING_APPROVAL and not for_approval:
        raise ResumeRefused("run is awaiting explicit human approval")
    if for_approval and manifest.status is not RunState.AWAITING_APPROVAL:
        raise ResumeRefused("run is not awaiting human approval")
    if automatic:
        if manifest.status is not RunState.PAUSED_USAGE or not manifest.auto_resume:
            raise ResumeRefused("run is not eligible for automatic usage-window resume")
        if manifest.pause is None or manifest.pause.next_attempt_at is None:
            raise ResumeRefused("usage reset time is unknown; resume manually")
        if now < manifest.pause.next_attempt_at:
            raise ResumeRefused(f"run is due at {manifest.pause.next_attempt_at.isoformat()}")
        adapter = registry.adapters.get(manifest.pause.provider)
        if adapter is None or not adapter.healthcheck().available:
            raise ResumeRefused("paused provider is unavailable or authentication needs attention")
        authenticated = getattr(adapter, "authenticated", None)
        if callable(authenticated) and not authenticated():
            raise ResumeRefused("paused provider is not signed in for subscription usage")
        probe = getattr(adapter, "observe_usage", None)
        if callable(probe):
            observed = probe()
            if manifest.pause.account_id and observed.account_id != manifest.pause.account_id:
                raise ResumeRefused("provider account changed or cannot be verified")
    if store.active_provider_alive():
        raise ResumeRefused("recorded provider process is still alive")
    latest_checkpoint = store.latest_checkpoint()
    allowed_unreceipted = (
        {"planning/task-contract.md"}
        if latest_checkpoint is not None and latest_checkpoint.get("next_stage") == "classifier"
        else set()
    )
    try:
        receipt = store.verify_checkpoint(allow_unreceipted_outputs=allowed_unreceipted)
    except CheckpointError as exc:
        raise ResumeRefused(str(exc)) from exc
    try:
        current_inputs = input_hashes(store, registry)
    except CheckpointError as exc:
        raise ResumeRefused(f"saved run input changed: {exc}") from exc
    if current_inputs != receipt["inputs"]:
        contract_path = "planning/task-contract.md"
        if (
            receipt.get("next_stage") == "classifier"
            and manifest.plan_origin == "task_contract"
            and manifest.approved_plan_version == "task-contract"
            and contract_path not in receipt["artifacts"]
        ):
            try:
                current_inputs = input_hashes(
                    store,
                    registry,
                    manifest_overrides={
                        "plan_origin": "generated",
                        "approved_plan_version": None,
                    },
                )
            except CheckpointError as exc:
                raise ResumeRefused(f"saved run input changed: {exc}") from exc
    if (
        current_inputs.get(".ai/project.yaml") != receipt["inputs"].get(".ai/project.yaml")
        and manifest.config_layers
        and project_config_matches(manifest.config_layers[0], project_config)
        and (
            dependency_discovery_default_omission(manifest.config_layers[0], project_config)
            or validation_files_default_omission(
                GitRepo(store.repo_root), manifest.git.base_commit or ""
            )
        )
    ):
        current_inputs[".ai/project.yaml"] = receipt["inputs"].get(".ai/project.yaml")
    saved_inputs = receipt["inputs"]
    if "validation_input_hashes" not in saved_inputs:
        current_inputs.pop("validation_input_hashes", None)
    if current_inputs != saved_inputs:
        raise ResumeRefused("saved request, policy, role binding, or context changed")
    if not manifest.config_layers or not project_config_matches(
        manifest.config_layers[0], project_config
    ):
        raise ResumeRefused("project configuration changed")
    try:
        same_repository = (
            GitRepo(store.repo_root).shared_root()
            == GitRepo(Path(manifest.repository)).shared_root()
        )
    except GitCommandError:
        same_repository = False
    if not same_repository:
        cloud = manifest.cloud_session
        if cloud is None:
            raise ResumeRefused("repository identity changed")
        try:
            identity = GitRepo(store.repo_root).remote_url()
        except Exception as exc:
            raise ResumeRefused("restored cloud repository identity is unavailable") from exc
        if identity != cloud.repository:
            raise ResumeRefused("restored cloud repository identity changed")
    if not worktree.is_dir():
        raise ResumeRefused(f"reserved run worktree is missing: {worktree}")
    worktree_repo = GitRepo(worktree)
    if worktree_repo.current_branch() != manifest.git.branch:
        raise ResumeRefused("run branch changed")
    if worktree_repo.current_commit() != receipt["worktree"]["head"]:
        raise ResumeRefused("run HEAD changed")
    if not manifest.git.base_commit or not worktree_repo.is_ancestor(manifest.git.base_commit):
        raise ResumeRefused("recorded base commit is missing from the worktree")
    fence = ScopeFence.from_config(project_config.scope)
    enforce_fence(worktree_repo, manifest.git.base_commit, fence)
    current = capture_snapshot(worktree_repo, manifest.git.base_commit)
    expected = receipt["worktree"]
    if manifest.pause and manifest.pause.snapshot_artifact:
        snapshot_path = store.root / manifest.pause.snapshot_artifact
        if not snapshot_path.is_file():
            raise ResumeRefused("pause snapshot is missing")
        expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if expected["digest"] != manifest.pause.worktree_digest:
            raise ResumeRefused("pause snapshot digest changed")
    if current != expected:
        if manifest.pause and manifest.pause.snapshot_artifact:
            raise ResumeRefused(
                "worktree differs from the saved pause snapshot; restore the recorded "
                f"run-owned changes (expected {expected['digest']}, found {current['digest']})"
            )
        if automatic or not adopt_worktree:
            raise ResumeRefused(
                f"worktree differs from checkpoint; inspect its diff and pass --adopt-worktree "
                f"only for run-owned changes (expected {expected['digest']}, found {current['digest']})"
            )
        store.write_json_artifact("execution/adopted-worktree.json", current, immutable=True)
        store.append_event({"event": "worktree_adopted", "digest": current["digest"]})
        store.record_human_action("edit")
    # Context is first resolved after classification; a run that paused before
    # then has none saved, so there is nothing to compare against.
    if receipt["next_stage"] != "classifier":
        runtime = resolve_runtime_context(
            store.repo_root, project_config, manifest.active_profiles, fence
        )
        if (
            runtime.included != manifest.context_included
            or runtime.excluded != manifest.context_excluded
            or runtime.conflicts != manifest.context_conflicts
            or runtime.minimum_tier != manifest.minimum_tier
        ):
            changes = [
                f"{name}: saved {saved!r}, now {now!r}"
                for name, saved, now in (
                    ("included", manifest.context_included, runtime.included),
                    ("excluded", manifest.context_excluded, runtime.excluded),
                    ("conflicts", manifest.context_conflicts, runtime.conflicts),
                    ("minimum_tier", manifest.minimum_tier, runtime.minimum_tier),
                )
                if saved != now
            ]
            raise ResumeRefused("resolved context changed; " + "; ".join(changes))
    if resolve_run_policy(manifest.config_layers) != manifest.resolved_config:
        raise ResumeRefused("protected policy changed")
    if manifest.approval_required and not for_approval:
        _verify_human_approval(store)
    return receipt


def _verify_human_approval(store: RunStore) -> dict:
    manifest = store.read_manifest()
    proof_path = store.root / "approval/approved.json"
    if not proof_path.is_file():
        raise ResumeRefused("human approval proof is missing")
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResumeRefused("human approval proof is invalid") from exc
    if not isinstance(proof, dict):
        raise ResumeRefused("human approval proof is invalid")
    session = manifest.cloud_session
    summary = store.root / "approval/human-approval.md"
    allowed_approval_runs = {manifest.run_id}
    ancestor_id = manifest.linked_source_run
    while ancestor_id and ancestor_id not in allowed_approval_runs:
        allowed_approval_runs.add(ancestor_id)
        ancestor = RunStore(store.repo_root, ancestor_id)
        if ancestor.root.resolve().parent != RunStore.runs_directory(store.repo_root).resolve():
            raise ResumeRefused("recovery lineage points outside this repository")
        if not (ancestor.root / "manifest.json").is_file():
            break
        ancestor_id = ancestor.read_manifest().linked_source_run
    if proof.get("run_id") not in allowed_approval_runs:
        raise ResumeRefused("human approval does not belong to this run or its recovery lineage")
    try:
        summary_digest = hashlib.sha256(summary.read_bytes()).hexdigest()
    except OSError as exc:
        raise ResumeRefused("human approval summary is missing") from exc
    expected = {
        "plan_sha256": manifest.plan_sha256,
        "approved_plan_version": manifest.approved_plan_version,
        "summary_sha256": summary_digest,
        "cloud_provider": session.provider if session else None,
        "cloud_session_id": session.session_id if session else None,
        "cloud_environment_id": session.environment_id if session else None,
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise ResumeRefused("human approval no longer matches the saved plan or cloud session")
    return proof


def approve_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    *,
    restored_worktree: Path | None = None,
) -> str:
    """Record a human's explicit approval of the pinned plan for a linked cloud run."""
    store = RunStore(repo.root, run_id)
    if not (store.root / "manifest.json").is_file():
        raise ResumeRefused(f"run {run_id!r} does not exist")
    with store.claim():
        manifest = store.read_manifest()
        if not manifest.goal_id:
            raise ResumeRefused(
                f"legacy run {run_id} has no goal ID; associate it with `dev-orch goal-associate` first"
            )
        session = manifest.cloud_session
        if session is None:
            raise ResumeRefused("link the intact cloud session before approving this run")
        if session.last_state in {"archived", "missing"}:
            raise ResumeRefused(f"linked cloud session is {session.last_state}")
        try:
            repository_identity = repo.remote_url()
        except GitCommandError:
            repository_identity = str(repo.root.resolve())
        if session.repository != repository_identity:
            raise ResumeRefused("linked cloud repository identity changed")
        if session.branch != manifest.git.branch:
            raise ResumeRefused("linked cloud branch changed")
        if not manifest.approval_required or not manifest.plan_sha256:
            raise ResumeRefused("run has no hash-pinned plan requiring human approval")
        worktree = restored_worktree or Path(manifest.git.worktree or "")
        receipt = _check_run(
            store,
            project_config,
            registry,
            worktree,
            automatic=False,
            adopt_worktree=False,
            now=datetime.now(UTC),
            for_approval=True,
        )
        if receipt["next_stage"] != "implementation_worker":
            raise ResumeRefused("approval checkpoint is not at the implementation gate")
        _approved(store)
        summary = store.root / "approval/human-approval.md"
        if not summary.is_file():
            raise ResumeRefused("human approval summary is missing")
        proof = store.root / "approval/approved.json"
        if proof.is_file():
            _verify_human_approval(store)
        else:
            store.write_json_artifact(
                "approval/approved.json",
                {
                    "run_id": manifest.run_id,
                    "plan_sha256": manifest.plan_sha256,
                    "approved_plan_version": manifest.approved_plan_version,
                    "summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
                    "cloud_provider": session.provider,
                    "cloud_session_id": session.session_id,
                    "cloud_environment_id": session.environment_id,
                    "approved_at": datetime.now(UTC).isoformat(),
                },
                immutable=True,
            )
        record_stage(
            store,
            registry,
            worktree,
            next_stage="implementation_worker",
            artifacts=["approval/human-approval.md", "approval/approved.json"],
            cycle=receipt["cycle"],
        )
        store.append_event(
            {"event": "human_approval_recorded", "plan_sha256": manifest.plan_sha256}
        )
        store.record_human_action("approval")
        Engine(store).transition(RunState.APPROVED)
        return hashlib.sha256(
            (store.root / store.read_manifest().checkpoint).read_bytes()
        ).hexdigest()


def resume_blocker(
    repo: GitRepo, config: ProjectConfig, registry: RoleRegistry, run_id: str
) -> str | None:
    """Explain a failed recovery gate without invoking a provider."""
    store = RunStore(repo.root, run_id)
    manifest = store.read_manifest()
    if manifest.checkpoint is None:
        return "legacy run needs explicit recovery"
    if manifest.auto_resume and manifest.pause is not None:
        adapter = registry.adapters.get(manifest.pause.provider)
        authenticated = getattr(adapter, "authenticated", None)
        if callable(authenticated) and not authenticated():
            return "paused provider is not signed in for subscription usage"
    try:
        _check_run(
            store,
            config,
            registry,
            Path(manifest.git.worktree or ""),
            automatic=False,
            adopt_worktree=False,
            now=datetime.now(UTC),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return str(exc)
    return None


def resume_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    *,
    automatic: bool = False,
    manual_resume: bool = True,
    adopt_worktree: bool = False,
    auto_resume: bool | None = None,
    now: datetime | None = None,
    restored_worktree: Path | None = None,
    scheduler_queue: SchedulerQueue | None = None,
    expected_checkpoint_digest: str | None = None,
) -> RunOutcome:
    store = RunStore(repo.root, run_id)
    if not (store.root / "manifest.json").is_file():
        raise ResumeRefused(f"run {run_id!r} does not exist")
    if not store.read_manifest().goal_id:
        raise ResumeRefused(
            f"legacy run {run_id} has no goal ID; associate it with `dev-orch goal-associate` first"
        )
    if store.read_manifest().checkpoint is None:
        if store.read_manifest().schema_version != "1.0":
            raise ResumeRefused("run initialization has no recovery checkpoint")
        if automatic or auto_resume:
            raise ResumeRefused("legacy runs require explicit manual recovery")
        recovered_id = recover_legacy(
            repo, project_config, registry, run_id, adopt_worktree=adopt_worktree
        )
        return resume_run(
            repo,
            project_config,
            registry,
            recovered_id,
            automatic=False,
            manual_resume=manual_resume,
            adopt_worktree=False,
            now=now,
            restored_worktree=restored_worktree,
            scheduler_queue=scheduler_queue,
        )
    with store.claim():
        manifest = store.read_manifest()
        dependency_report = ""
        if project_config.context.dependency_discovery:
            dependency_path = store.root / "dependency-report.json"
            if dependency_path.is_file():
                dependency_report = json.dumps(
                    json.loads(dependency_path.read_text(encoding="utf-8")), indent=2
                )
        if manifest.validation_input_hashes:
            current_hashes = validation_file_hashes(repo, project_config.validation_files)
            if current_hashes != manifest.validation_input_hashes:
                raise ResumeRefused(
                    "declared validation files changed since the run began; restore the recorded "
                    "bytes for recovery, or start a fresh run for intentionally different inputs"
                )
        if (
            manifest.status is RunState.APPROVED
            and manifest.cloud_session is not None
            and expected_checkpoint_digest is None
        ):
            raise ResumeRefused(
                "approved cloud run needs the checkpoint digest shown by `dev-orch approve`"
            )
        if expected_checkpoint_digest is not None:
            if not manifest.checkpoint:
                raise ResumeRefused("cloud checkpoint is missing")
            actual_digest = hashlib.sha256(
                (store.root / manifest.checkpoint).read_bytes()
            ).hexdigest()
            if actual_digest != expected_checkpoint_digest:
                raise ResumeRefused("cloud checkpoint digest changed")
        worktree = restored_worktree or Path(manifest.git.worktree or "")
        receipt = _check_run(
            store,
            project_config,
            registry,
            worktree,
            automatic=automatic,
            adopt_worktree=adopt_worktree,
            now=now or datetime.now(UTC),
        )
        if (
            project_config.validation_files
            and not manifest.validation_input_hashes
            and receipt["next_stage"] != "classifier"
        ):
            raise ResumeRefused("saved validation input hashes are missing")
        if manifest.retry_of is not None:
            # Parse the immutable child snapshot only after checkpoint hashing has
            # verified it; resumed stages never consult the mutable source run.
            prior_failures_context(store)
        if auto_resume is not None and not automatic:
            store.update_manifest(auto_resume=auto_resume)
            manifest = store.read_manifest()
        adapters = {
            name: UsageAwareAdapter(adapter, auto_resume=manifest.auto_resume, store=store)
            for name, adapter in registry.adapters.items()
        }
        registry = RoleRegistry(registry.roles, adapters)
        base = manifest.git.base_commit
        assert base is not None
        fence = ScopeFence.from_config(project_config.scope)
        worktree_repo = GitRepo(worktree)
        engine = Engine(store)
        stage = receipt["next_stage"]
        cycle = receipt["cycle"]
        if stage not in _STAGE_STATE:
            raise ResumeRefused(f"unknown saved next stage {stage!r}")
        queued_cloud_resume = (
            expected_checkpoint_digest is not None
            and manifest.cloud_session is not None
            and expected_checkpoint_digest == manifest.cloud_session.last_delivery_digest
        )
        if manual_resume and not automatic and not queued_cloud_resume:
            store.record_human_action("resume")
        stage_role = {
            "validation": "validator",
            "plan_finalization": "orchestrator",
            "commit": "verifier",
            "validation_repair": "implementation_worker",
            "remediation": "implementation_worker",
        }.get(stage, stage)
        store.update_manifest(
            status=_STAGE_STATE[stage],
            transition_role=stage_role,
            pause=None,
            terminal_reason=None,
            stop=None,
        )
        store.append_event(
            {"event": "run_resumed", "stage": stage, "automatic": automatic or queued_cloud_resume}
        )

        def checkpoint(next_stage: str, artifacts: list[str], cycle_number: int = cycle) -> None:
            if store.read_manifest().seed_patch_artifact:
                artifacts = [*artifacts, SEED_PATCH_ARTIFACT, SEED_PATCH_RECEIPT]
            record_stage(
                store,
                registry,
                worktree,
                next_stage=next_stage,
                artifacts=artifacts,
                cycle=cycle_number,
            )

        try:
            if (
                project_config.context.dependency_discovery
                and not dependency_report
                and stage != "classifier"
            ):
                inventory, metadata = dependency_sources(repo.root, project_config)
                dependency_runtime = resolve_runtime_context(
                    repo.root, project_config, manifest.active_profiles, fence
                )
                dependency_report = discover_dependencies(
                    registry,
                    manifest.request_text or "",
                    inventory,
                    metadata,
                    dependency_runtime.invariants,
                    store,
                )
            while True:
                current = store.read_manifest()
                request_text = current.request_text or ""
                criteria = current.acceptance_criteria
                runtime = resolve_runtime_context(
                    repo.root, project_config, current.active_profiles, fence
                )
                protected = ContextRef(
                    label=Category.INVARIANTS,
                    path="framework:protected",
                    content=json.dumps(current.resolved_config, sort_keys=True),
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
                if stage == "classifier":
                    classifier_references = classification_references_for_run(
                        store,
                        project_config,
                        request_text,
                        fence,
                        persist_if_missing=False,
                    )
                    classification = classify(
                        registry,
                        request_text,
                        current.project_class,
                        store,
                        current.available_profiles,
                        classifier_references,
                        criteria,
                    )
                    specification_minimum, specification_reason = specification_minimum_tier(
                        request_text
                    )
                    if specification_minimum is not None and _tier_rank(
                        classification.tier
                    ) < _tier_rank(specification_minimum):
                        classification = classification.model_copy(
                            update={
                                "tier": specification_minimum,
                                "rationale": f"{classification.rationale}; raised to standard because {specification_reason}",
                            }
                        )
                        store.write_json_artifact(
                            "classification.json", classification.model_dump(mode="json")
                        )
                        store.append_event(
                            {
                                "event": "classification_tier_raised",
                                "tier": str(specification_minimum),
                                "reason": specification_reason,
                            }
                        )
                    engine.transition(RunState.CLASSIFIED)
                    runtime = resolve_runtime_context(
                        repo.root, project_config, classification.profiles, fence
                    )
                    if project_config.context.dependency_discovery and not dependency_report:
                        inventory, metadata = dependency_sources(repo.root, project_config)
                        dependency_report = discover_dependencies(
                            registry,
                            request_text,
                            inventory,
                            metadata,
                            runtime.invariants,
                            store,
                        )
                    layers = [
                        project_config.model_dump(mode="json"),
                        *[
                            {"profiles": {name: definition.model_dump(mode="json")}}
                            for name, definition in project_config.profiles.definitions.items()
                            if name in classification.profiles
                        ],
                    ]
                    if current.tier_override:
                        layers.append({"run": {"tier_override": str(current.tier_override)}})
                    policy = resolve_run_policy(layers)
                    effective = _effective_tier(
                        classification.tier,
                        current.tier_override,
                        runtime.minimum_tier,
                        current.downgrade_reason,
                    )
                    store.update_manifest(
                        tier=effective,
                        active_profiles=classification.profiles,
                        minimum_tier=runtime.minimum_tier,
                        context_included=runtime.included,
                        context_excluded=runtime.excluded,
                        context_conflicts=runtime.conflicts,
                        config_layers=layers,
                        resolved_config=policy,
                    )
                    protected = ContextRef(
                        label=Category.INVARIANTS,
                        path="framework:protected",
                        content=json.dumps(policy, sort_keys=True),
                    )
                    invariants = [*runtime.invariants, protected]
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
                    input_hashes = validation_file_hashes(repo, project_config.validation_files)
                    if (
                        current.validation_input_hashes
                        and input_hashes != current.validation_input_hashes
                    ):
                        raise ValidationEnvironmentError(
                            "declared validation files changed since the run began; restart from validation"
                        )
                    store.update_manifest(validation_input_hashes=input_hashes)
                    stage = (
                        "planner"
                        if effective in {Tier.STANDARD, Tier.SUBSTANTIAL}
                        else "implementation_worker"
                    )
                    artifacts = ["classification.json", "context.json"]
                    if stage == "implementation_worker":
                        checkpoint("classifier", artifacts)
                        approved = _task_contract(
                            request_text, project_config, criteria, invariants
                        )
                        _persist_task_contract(store, approved)
                        artifacts.append("planning/task-contract.md")
                    checkpoint(stage, artifacts)
                elif stage == "planner":
                    if current.external_plan_artifact:
                        version, _ = store.import_external_plan(
                            store.root / current.external_plan_artifact
                        )
                    else:
                        version = plan(
                            registry,
                            request_text,
                            invariants,
                            runtime.references,
                            store,
                            scope_fence=scope_context,
                            acceptance_criteria=criteria,
                            dependency_report=dependency_report,
                        )
                    store.update_manifest(
                        status=RunState.PLANNED,
                        transition_role="orchestrator" if current.external_plan_artifact else None,
                    )
                    checkpoint("plan_reviewer", [f"planning/{version.name}"])
                    stage = "plan_reviewer"
                elif stage == "plan_reviewer":
                    version = _latest_plan(store)
                    reviewed = review_plan(
                        registry,
                        request_text,
                        version.read_text(encoding="utf-8"),
                        runtime.profile_constraints,
                        store,
                        criteria,
                        scope_context,
                        dependency_report,
                    )
                    store.update_manifest(status=RunState.PLAN_REVIEWED)
                    stage = "plan_reconciler" if reviewed.blocking_ids() else "plan_finalization"
                    checkpoint(
                        stage,
                        [
                            str(_latest(store, "review", "plan-review").relative_to(store.root)),
                            "review/finding-index.json",
                        ],
                    )
                elif stage == "plan_reconciler":
                    reviewed = ReviewResult.model_validate_json(
                        _latest(store, "review", "plan-review").read_text(encoding="utf-8")
                    )
                    if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                        target = (
                            RunState.BLOCKED
                            if reviewed.outcome is Outcome.BLOCKED
                            else RunState.ESCALATED
                        )
                        _terminal(
                            engine,
                            target,
                            f"plan review: {reviewed.outcome}",
                            "plan_review_escalated"
                            if reviewed.outcome is Outcome.ESCALATE
                            else "other",
                            stop_code="plan_review_blocked" if target is RunState.BLOCKED else None,
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    cycle += 1
                    if cycle > MAX_REMEDIATION_CYCLES:
                        _terminal(
                            engine,
                            RunState.ESCALATED,
                            "plan reconciliation budget exhausted",
                            stop_code="plan_review_exhausted",
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    version = reconcile(
                        registry,
                        _latest_plan(store).read_text(encoding="utf-8"),
                        reviewed.findings,
                        store,
                        request_text,
                        criteria,
                        scope_context,
                        dependency_report,
                        cycle=cycle,
                    )
                    store.update_manifest(
                        status=RunState.PLANNED, transition_role="plan_reconciler"
                    )
                    checkpoint("plan_reviewer", [f"planning/{version.name}"], cycle)
                    stage = "plan_reviewer"
                elif stage == "plan_finalization":
                    reviewed = ReviewResult.model_validate_json(
                        _latest(store, "review", "plan-review").read_text(encoding="utf-8")
                    )
                    if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                        target = (
                            RunState.BLOCKED
                            if reviewed.outcome is Outcome.BLOCKED
                            else RunState.ESCALATED
                        )
                        _terminal(
                            engine,
                            target,
                            f"plan review: {reviewed.outcome}",
                            "plan_review_escalated"
                            if reviewed.outcome is Outcome.ESCALATE
                            else "other",
                            stop_code="plan_review_blocked" if target is RunState.BLOCKED else None,
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    store.approve_plan(_latest_plan(store))
                    final_review = _latest(store, "review", "plan-review")
                    create_plan_review_receipt(
                        store,
                        _latest_plan(store),
                        final_review,
                        runtime.profile_constraints,
                        _dependency_review_identity(store),
                    )
                    approval_required = _approval_required(
                        project_config, current.tier, current.active_profiles
                    )
                    store.update_manifest(
                        status=RunState.PLAN_FINALIZED, approval_required=approval_required
                    )
                    checkpoint(
                        "implementation_worker",
                        ["planning/approved-plan.md", PLAN_REVIEW_RECEIPT],
                    )
                    stage = "implementation_worker"
                elif stage == "implementation_worker":
                    approved = _approved(store)
                    approval_required = _approval_required(
                        project_config, current.tier, current.active_profiles
                    )
                    if current.approval_required != approval_required:
                        store.update_manifest(approval_required=approval_required)
                        checkpoint(
                            "implementation_worker",
                            [
                                "planning/task-contract.md"
                                if current.plan_origin == "task_contract"
                                else "planning/approved-plan.md"
                            ],
                        )
                    if approval_required and not (store.root / "approval/approved.json").is_file():
                        store.write_text_artifact(
                            "approval/human-approval.md",
                            _approval_summary(
                                request_text,
                                project_config,
                                current.tier,
                                current.active_profiles,
                                approved,
                            ),
                            immutable=True,
                        )
                        store.update_manifest(
                            status=RunState.AWAITING_APPROVAL, approval_required=True
                        )
                        checkpoint("implementation_worker", ["approval/human-approval.md"])
                        return _outcome(
                            engine,
                            reason="human approval required before execution",
                            repo=repo,
                            worktree=worktree,
                        )
                    try:
                        seed_patch_applied = apply_seed_patch_once(
                            store, worktree_repo, base, fence
                        )
                    except ScopeViolation as exc:
                        _terminal(engine, RunState.ESCALATED, str(exc))
                        return _outcome(engine, repo=repo, worktree=worktree)
                    except GitCommandError as exc:
                        _terminal(
                            engine,
                            RunState.FAILED,
                            f"saved seed patch could not be applied: {exc}",
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    if seed_patch_applied:
                        checkpoint(
                            "implementation_worker", [SEED_PATCH_ARTIFACT, SEED_PATCH_RECEIPT]
                        )
                    store.update_manifest(status=RunState.EXECUTING)
                    events_path = store.root / "events.jsonl"
                    continued = (
                        any(
                            json.loads(line).get("event") == "change_continued"
                            for line in events_path.read_text(encoding="utf-8").splitlines()
                        )
                        if events_path.is_file()
                        else False
                    )
                    if (store.root / CONTINUED_CHANGE_ARTIFACT).is_file() and not continued:
                        try:
                            applied = apply_continued_change(store, worktree_repo, base, fence)
                            if not applied:
                                raise CheckpointError("continued change patch is missing")
                        except GitCommandError as exc:
                            _terminal(
                                engine,
                                RunState.ESCALATED,
                                f"the saved change no longer applies to the current repository "
                                f"({exc}); use `dev-orch retry --fresh` to re-implement",
                            )
                            return _outcome(engine, repo=repo, worktree=worktree)
                        store.append_event(
                            {"event": "change_continued", "source_run": current.retry_of}
                        )
                        checkpoint("validation", [CONTINUED_CHANGE_ARTIFACT])
                    else:
                        before_worker = _deliverable_patch(worktree_repo, base)
                        _run_worktree_stage(
                            worktree_repo,
                            base,
                            fence,
                            lambda approved=approved, invariants=invariants, runtime=runtime, scope_context=scope_context, dependency_report=dependency_report: (
                                execute(
                                    registry,
                                    approved,
                                    invariants,
                                    runtime.references,
                                    worktree,
                                    store,
                                    runtime.profile_constraints,
                                    scope_context,
                                    dependency_report,
                                )
                            ),
                        )
                        after_worker = _deliverable_patch(worktree_repo, base)
                        if not after_worker:
                            _record_no_progress(
                                engine,
                                cause="worker_no_changes",
                                stage="implementation_worker",
                                before=before_worker,
                                after=after_worker,
                            )
                            return _outcome(engine, repo=repo, worktree=worktree)
                        checkpoint("validation", ["execution/worker-result.json"])
                    stage = "validation"
                elif stage == "validation":
                    store.update_manifest(status=RunState.VALIDATING)
                    outcomes = _run_validation_stage(
                        worktree_repo,
                        base,
                        fence,
                        lambda current=current: run_validations(
                            project_config.validation, current.tier, worktree
                        ),
                        repo.root,
                        tuple(project_config.validation_links),
                        tuple(project_config.required_validation_links),
                        tuple(project_config.validation_files),
                        current.validation_input_hashes,
                    )
                    artifact = _save_validation(store, outcomes, cycle)
                    if not all_passed(outcomes):
                        if current.restart_mode == "check_only":
                            _terminal(
                                engine,
                                RunState.FAILED,
                                "required validation failed during restart",
                                "restart_validation_failed",
                            )
                            return _outcome(engine, repo=repo, worktree=worktree)
                        if current.validation_repairs_reserved >= 1:
                            _terminal(
                                engine,
                                RunState.FAILED,
                                "required validation failed after repair",
                                "validation_failed",
                            )
                            return _outcome(engine, repo=repo, worktree=worktree)
                        store.update_manifest(validation_repairs_reserved=1)
                        checkpoint("validation_repair", [artifact], cycle)
                        stage = "validation_repair"
                        continue
                    stage = (
                        "implementation_reviewer"
                        if _implementation_review_required(current.tier, project_config.validation)
                        else "verifier"
                    )
                    checkpoint(stage, [artifact])
                elif stage == "validation_repair":
                    outcomes = _validation(store, cycle)
                    if all_passed(outcomes):
                        raise ResumeRefused("validation repair checkpoint has no failed checks")
                    current = store.read_manifest()
                    if current.validation_repairs_reserved != 1:
                        raise ResumeRefused("validation repair was not reserved")
                    store.update_manifest(status=RunState.REMEDIATION)
                    _run_worktree_stage(
                        worktree_repo,
                        base,
                        fence,
                        lambda outcomes=outcomes, dependency_report=dependency_report: (
                            repair_validation(
                                registry,
                                _approved(store),
                                worktree_repo.change_diff(base),
                                outcomes,
                                worktree,
                                store,
                                dependency_report,
                            )
                        ),
                    )
                    checkpoint("validation", ["execution/validation-repair-v1.json"], cycle)
                    stage = "validation"
                elif stage == "implementation_reviewer":
                    store.update_manifest(status=RunState.IMPLEMENTATION_REVIEW)
                    outcomes = _validation(store, cycle)
                    before = capture_snapshot(worktree_repo, base)
                    reviewed = review_implementation(
                        registry,
                        _approved(store),
                        worktree_repo.change_diff(base),
                        _evidence(outcomes),
                        runtime.profile_constraints,
                        store,
                        worktree,
                        dependency_report,
                    )
                    cleanup_ephemeral_paths(worktree)
                    require_snapshot(worktree_repo, before)
                    if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                        target = (
                            RunState.BLOCKED
                            if reviewed.outcome is Outcome.BLOCKED
                            else RunState.ESCALATED
                        )
                        _terminal(
                            engine,
                            target,
                            f"implementation review: {reviewed.outcome}",
                            "implementation_review_escalated"
                            if reviewed.outcome is Outcome.ESCALATE
                            else "other",
                            stop_code="implementation_review_blocked"
                            if target is RunState.BLOCKED
                            else None,
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    stage = "remediation" if reviewed.blocking_ids() else "verifier"
                    checkpoint(
                        stage,
                        [
                            str(
                                _latest(store, "review", "implementation-review").relative_to(
                                    store.root
                                )
                            ),
                            "review/finding-index.json",
                        ],
                    )
                    if current.restart_mode == "check_only" and reviewed.blocking_ids():
                        _terminal(
                            engine,
                            RunState.FAILED,
                            "implementation review found blocking issues during check-only restart",
                            "restart_review_findings",
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                elif stage == "remediation":
                    reviewed = ReviewResult.model_validate_json(
                        _latest(store, "review", "implementation-review").read_text(
                            encoding="utf-8"
                        )
                    )
                    if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                        target = (
                            RunState.BLOCKED
                            if reviewed.outcome is Outcome.BLOCKED
                            else RunState.ESCALATED
                        )
                        _terminal(
                            engine,
                            target,
                            f"implementation review: {reviewed.outcome}",
                            "implementation_review_escalated"
                            if reviewed.outcome is Outcome.ESCALATE
                            else "other",
                            stop_code="implementation_review_blocked"
                            if target is RunState.BLOCKED
                            else None,
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    cycle += 1
                    if cycle > MAX_REMEDIATION_CYCLES:
                        _terminal(
                            engine,
                            RunState.ESCALATED,
                            "remediation budget exhausted",
                            stop_code="implementation_review_exhausted",
                        )
                        return _outcome(engine, repo=repo, worktree=worktree)
                    store.update_manifest(status=RunState.REMEDIATION)
                    blocking = [f for f in reviewed.findings if f.id in reviewed.blocking_ids()]
                    before_remediation = _deliverable_patch(worktree_repo, base)
                    _run_worktree_stage(
                        worktree_repo,
                        base,
                        fence,
                        lambda blocking=blocking, cycle=cycle, dependency_report=dependency_report: (
                            remediate(
                                registry,
                                blocking,
                                _approved(store),
                                worktree_repo.change_diff(base),
                                worktree,
                                store,
                                cycle=cycle,
                                dependency_report=dependency_report,
                            )
                        ),
                    )
                    after_remediation = _deliverable_patch(worktree_repo, base)
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
                    stage = "validation"
                    checkpoint(stage, [f"execution/remediation-v{cycle}.json"], cycle)
                elif stage == "verifier":
                    store.update_manifest(status=RunState.FINAL_VERIFICATION)
                    with validation_environment(
                        repo.root,
                        worktree,
                        tuple(project_config.validation_links),
                        tuple(project_config.required_validation_links),
                    ):
                        pass
                    outcomes = _validation(store, cycle)
                    review_paths = list(
                        (store.root / "review").glob("implementation-review-v*.json")
                    )
                    reviewed = (
                        ReviewResult.model_validate_json(
                            _latest(store, "review", "implementation-review").read_text(
                                encoding="utf-8"
                            )
                        )
                        if review_paths
                        else None
                    )
                    before = capture_snapshot(worktree_repo, base)
                    verification = verify(
                        registry,
                        request_text,
                        _approved(store),
                        criteria,
                        worktree_repo.change_diff(base),
                        _evidence(outcomes),
                        reviewed.findings if reviewed else [],
                        store,
                        worktree,
                        dependency_report,
                    )
                    cleanup_ephemeral_paths(worktree)
                    require_snapshot(worktree_repo, before)
                    stage = "commit"
                    checkpoint(stage, ["verification/final-verification.json"])
                elif stage == "commit":
                    verification = Verification.model_validate_json(
                        (store.root / "verification/final-verification.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    reviewed_paths = list(
                        (store.root / "review").glob("implementation-review-v*.json")
                    )
                    reviewed = (
                        ReviewResult.model_validate_json(
                            _latest(store, "review", "implementation-review").read_text(
                                encoding="utf-8"
                            )
                        )
                        if reviewed_paths
                        else None
                    )
                    if not verification_allows_completion(
                        verification,
                        criteria,
                        reviewed.findings if reviewed else [],
                    ):
                        _terminal(
                            engine,
                            RunState.FAILED,
                            "final verification did not pass",
                            "restart_verification_failed"
                            if current.restart_mode is not None
                            else "other",
                            stop_code="verification_rejected",
                        )
                        return _outcome(engine, verification, repo=repo, worktree=worktree)
                    enforce_fence(worktree_repo, base, fence)
                    changed = worktree_repo.change_inventory(base)
                    if not changed:
                        _terminal(
                            engine,
                            RunState.FAILED,
                            "run produced no change to commit",
                            stop_code="worker_no_changes",
                        )
                        return _outcome(engine, verification, repo=repo, worktree=worktree)
                    final_commit = worktree_repo.commit_snapshot(
                        f"ai({run_id}): complete orchestrated run", changed
                    )
                    worktree_repo.ensure_clean()
                    store.update_manifest(
                        git=GitBlock(
                            base_commit=base,
                            branch=current.git.branch,
                            worktree=str(worktree),
                            final_commit=final_commit,
                        )
                    )
                    store.write_json_artifact(
                        "execution/execution-summary.json",
                        {
                            "changed_files": changed,
                            "base_commit": base,
                            "final_commit": final_commit,
                        },
                    )
                    engine.transition(RunState.COMPLETE_LOCAL)
                    if current.auto_resume or scheduler_queue is not None:
                        (scheduler_queue or SchedulerQueue()).cancel(repo.root, run_id)
                    return _outcome(engine, verification, repo=repo, worktree=worktree)
                else:
                    raise ResumeRefused(f"unsupported stage {stage}")
        except (UsageLimitError, AgentTimeoutError, AgentInterruptedError) as exc:
            enforce_fence(worktree_repo, base, fence)
            snapshot = capture_snapshot(worktree_repo, base)
            path = f"execution/pause-snapshot-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.json"
            store.write_json_artifact(path, snapshot, immutable=True)
            if isinstance(exc, UsageLimitError):
                observation = exc.observation
                pause = PauseRecord(
                    provider=observation.provider,
                    role=exc.role,
                    limit_kind=observation.limit_kind,
                    observed_at=observation.observed_at,
                    reset_at=observation.reset_at,
                    next_attempt_at=next_attempt(observation),
                    retry_count=(manifest.pause.retry_count + 1) if manifest.pause else 1,
                    diagnostic=observation.diagnostic,
                    worktree_digest=snapshot["digest"],
                    snapshot_artifact=path,
                    account_id=observation.account_id,
                    raw_diagnostic_ref=observation.raw_diagnostic_ref,
                )
                store.update_manifest(pause=pause, status=RunState.PAUSED_USAGE)
                if manifest.auto_resume and pause.next_attempt_at is not None:
                    (scheduler_queue or SchedulerQueue()).register(
                        repo.root, run_id, pause.next_attempt_at
                    )
            else:
                store.update_manifest(
                    pause=PauseRecord(
                        provider=exc.provider,
                        role=exc.role,
                        observed_at=datetime.now(UTC),
                        diagnostic=str(exc),
                        worktree_digest=snapshot["digest"],
                        snapshot_artifact=path,
                    ),
                    status=RunState.PAUSED_INTERRUPTED,
                )
            return _outcome(engine, repo=repo, worktree=worktree)
        except ScopeViolation as exc:
            if store.read_manifest().restart_mode is None:
                store.record_stop("scope_violation")
                raise
            _terminal(
                engine,
                RunState.ESCALATED,
                str(exc),
                "restart_governance_blocked",
                stop_code="scope_violation",
            )
            return _outcome(engine, repo=repo, worktree=worktree)
        except RemediationExhausted as exc:
            if store.read_manifest().restart_mode is None:
                store.record_stop(exc.cause)
                raise
            _terminal(engine, RunState.ESCALATED, str(exc), "restart_remediation_exhausted")
            return _outcome(engine, repo=repo, worktree=worktree)
        except ValidationEnvironmentError as exc:
            _terminal(engine, RunState.FAILED, str(exc), "environment")
            return _outcome(engine, repo=repo, worktree=worktree)
        except AgentInvocationError as exc:
            _terminal(
                engine,
                RunState.ESCALATED,
                str(exc),
                "environment",
                failure_class="environment",
            )
            return _outcome(engine, repo=repo, worktree=worktree)
        except (OSError, subprocess.SubprocessError) as exc:
            _terminal(
                engine,
                RunState.ESCALATED,
                f"provider invocation failed: {exc}",
                "environment",
                failure_class="environment",
            )
            return _outcome(engine, repo=repo, worktree=worktree)
        except SchemaEscalation:
            store.record_stop("schema_rejected")
            raise
        except (ContextContractError, CheckpointError, ResumeRefused) as exc:
            restart_mode = store.read_manifest().restart_mode
            cause = "other"
            if restart_mode is not None:
                detail = str(exc).lower()
                cause = (
                    "restart_governance_blocked"
                    if any(word in detail for word in ("scope", "approval", "policy", "protected"))
                    else "restart_evidence_invalid"
                )
            stop_code = "prompt_budget" if isinstance(exc, PromptBudgetError) else None
            _terminal(engine, RunState.ESCALATED, str(exc), cause, stop_code=stop_code)
            return _outcome(engine, repo=repo, worktree=worktree)
        except BaseException:
            store.record_stop("other")
            raise


def _validation(store: RunStore, cycle: int) -> list[ValidationOutcome]:
    artifact = store.read_manifest().validation_artifact or (
        f"execution/validation-v{cycle + 1}.json"
    )
    path = store.root / artifact
    if not path.is_file():
        raise ResumeRefused(f"validation artifact {path.name} is missing")
    return [ValidationOutcome.model_validate(item) for item in json.loads(path.read_text())]


def resume_due(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    *,
    now: datetime | None = None,
    restored_worktree: Path | None = None,
    scheduler_queue: SchedulerQueue | None = None,
    cloud_gateway=None,
) -> object:
    """Scheduler-neutral, idempotent wakeup with one atomic run claim."""
    manifest = RunStore(repo.root, run_id).read_manifest()
    if manifest.cloud_session is not None:
        from dev_orchestration.workflow.cloud import (
            ClaudeCloudGateway,
            CodexCloudGateway,
            resume_cloud_due,
        )

        gateway = cloud_gateway or (
            ClaudeCloudGateway()
            if manifest.cloud_session.provider == "claude"
            else CodexCloudGateway()
        )
        delivery = resume_cloud_due(repo, run_id, gateway, now=now)
        queue = scheduler_queue or SchedulerQueue()
        if delivery.accepted:
            queue.cancel(repo.root, run_id)
        else:
            pause = RunStore(repo.root, run_id).read_manifest().pause
            if pause and pause.next_attempt_at:
                queue.register(repo.root, run_id, pause.next_attempt_at)
            else:
                queue.cancel(repo.root, run_id)
        return delivery
    return resume_run(
        repo,
        project_config,
        registry,
        run_id,
        automatic=True,
        now=now,
        restored_worktree=restored_worktree,
        scheduler_queue=scheduler_queue,
    )
