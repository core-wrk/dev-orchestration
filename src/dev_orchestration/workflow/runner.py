"""Drive one local run through the M2 workflow and its safety gates."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.adapters.base import AgentInterruptedError, AgentTimeoutError
from dev_orchestration.adapters.registry import ReadOnlyRoleUnsupportedError, RoleRegistry
from dev_orchestration.adapters.usage import UsageAwareAdapter, UsageLimitError, next_attempt
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig, ValidationCommand
from dev_orchestration.config.resolver import PROTECTED_DEFAULTS
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, RunState, Tier
from dev_orchestration.domain.findings import Verification
from dev_orchestration.domain.run import GitBlock, PauseRecord, RoleBinding
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
from dev_orchestration.workflow.engine import Engine, IllegalTransitionError
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
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
from dev_orchestration.workflow.snapshot import capture_snapshot
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    FindingIdentityAmbiguity,
    RemediationExhausted,
    classify,
    execute,
    plan,
    reconcile,
    remediate,
    repair_validation,
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
    ValidationOutcome,
    all_passed,
    run_validations,
    validation_environment,
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


def _validation_json(outcomes: list[ValidationOutcome]) -> list[dict]:
    return [outcome.model_dump(mode="json") for outcome in outcomes]


def _save_validation(store: RunStore, outcomes: list[ValidationOutcome], cycle: int = 0) -> str:
    versions = []
    for path in (store.root / "execution").glob("validation-v*.json"):
        try:
            versions.append(int(path.stem.rsplit("-v", 1)[1]))
        except ValueError:
            continue
    artifact = f"execution/validation-v{max(versions, default=0) + 1}.json"
    records = _validation_json(outcomes)
    store.write_json_artifact(artifact, records, immutable=True)
    store.update_manifest(validation_artifact=artifact)
    store.append_event(
        {
            "event": "validated",
            "cycle": cycle,
            "passed": all_passed(outcomes),
            "required_commands": [outcome.name for outcome in outcomes],
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
        roles[role] = RoleBinding(
            adapter=binding.adapter,
            model_alias=binding.model,
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


def _terminal(
    engine: Engine,
    state: RunState,
    reason: str,
    cause: str = "other",
) -> None:
    store = engine.store
    store.update_manifest(terminal_reason=reason, terminal_cause=cause)
    store.append_event(
        {"event": "run_terminal", "state": str(state), "detail": reason, "cause": cause}
    )
    if engine.state not in {
        RunState.COMPLETE_LOCAL,
        RunState.ESCALATED,
        RunState.FAILED,
        RunState.CANCELLED,
    }:
        engine.transition(state)


# COMPLETE_LOCAL is deliberately excluded from cleanup: that worktree and its
# branch are the run's deliverable, left for a human to review, diff, and
# merge by hand. Only the terminal states that produced nothing worth keeping
# checked out are swept.
_UNPRODUCTIVE_TERMINAL_STATES = frozenset({RunState.FAILED, RunState.ESCALATED, RunState.CANCELLED})


def cleanup_worktree_if_unproductive(engine: Engine, repo: GitRepo, worktree: Path) -> None:
    """Remove a run's worktree once nothing further can happen in it.

    Left in place, unproductive worktrees accumulate indefinitely (nothing
    else ever removes them) and each one adds objects to the shared git
    database. A large backlog of unreachable objects has been observed to
    make plain `git` calls -- including ones agents run unprompted while
    exploring a repo -- slow enough to blow through provider timeouts.
    A worktree with uncommitted changes is left alone and merely noted:
    dev-orch never modifies or discards uncommitted work (see
    GitRepo.ensure_clean), and an unproductive run can still have left real,
    unfinished output behind for a human to recover by hand.
    """
    if engine.state not in _UNPRODUCTIVE_TERMINAL_STATES or not worktree.exists():
        return
    if GitRepo(worktree).status_paths():
        engine.store.append_event(
            {
                "event": "worktree_cleanup_skipped",
                "detail": f"{worktree} has uncommitted changes; left in place for manual review",
            }
        )
        return
    try:
        remove_worktree(repo, worktree)
    except GitCommandError as exc:
        engine.store.append_event({"event": "worktree_cleanup_failed", "detail": str(exc)})
        return
    engine.store.append_event({"event": "worktree_removed", "path": str(worktree)})


# Causes where the plan was fine and the code was close: the change is kept so a retry
# can continue from it instead of re-implementing.
CHANGE_PATCH_ARTIFACT = "execution/final-change.patch"
_PATCH_WORTHY_CAUSES = frozenset(
    {
        "implementation_review_exhausted",
        "implementation_review_escalated",
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
    patch_text = patch_bytes.decode("utf-8")
    _run_worktree_stage(
        worktree_repo,
        base_commit,
        fence,
        lambda: worktree_repo.apply_patch(patch_text),
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


def _run_worktree_stage(
    worktree_repo: GitRepo,
    base_commit: str,
    fence: ScopeFence,
    action,
):
    """Run an agent stage and restore all ignored side effects afterward."""
    ignored_before = worktree_repo.ignored_paths()
    try:
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
):
    """Run validation while ensuring its ignored caches do not become residue."""
    ignored_before = worktree_repo.ignored_paths()
    try:
        with validation_environment(validation_root, worktree_repo.root, link_paths):
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
) -> RunOutcome:
    """Execute a run; every started run is returned in a terminal state."""
    if not request_text.strip():
        raise ContextContractError("a run requires non-empty request text")
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
        if retry_source.repo_root.resolve() != repo.root.resolve():
            raise ContextContractError("retry evidence source belongs to a different repository")
    store, worktree = bootstrap_run(
        repo, project_config, request_text, Tier.STANDARD, worktree_root
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
        if continue_patch is not None:
            store.write_text_artifact(
                CONTINUED_CHANGE_ARTIFACT,
                continue_patch.read_text(encoding="utf-8"),
                immutable=True,
            )

        def receipt(next_stage: str, artifacts: list[str], cycle: int = 0) -> None:
            record_stage(
                store,
                registry,
                worktree,
                next_stage=next_stage,
                artifacts=artifacts,
                cycle=cycle,
            )

        def repair_failed_validation(
            outcomes: list[ValidationOutcome], cycle: int, next_stage: str
        ) -> tuple[list[ValidationOutcome], bool]:
            if all_passed(outcomes):
                artifact = store.read_manifest().validation_artifact
                if artifact is None:
                    raise CheckpointError("passed validation has no saved artifact pointer")
                receipt(next_stage, [artifact], cycle)
                return outcomes, False
            if store.read_manifest().validation_repairs_reserved >= 1:
                return outcomes, True
            store.update_manifest(validation_repairs_reserved=1)
            failed_artifact = store.read_manifest().validation_artifact
            if failed_artifact is None:
                raise CheckpointError("failed validation has no saved artifact pointer")
            receipt("validation_repair", [failed_artifact], cycle)
            engine.transition(RunState.REMEDIATION)
            _run_worktree_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: repair_validation(
                    registry,
                    approved,
                    worktree_repo.change_diff(base_commit),
                    outcomes,
                    worktree,
                    store,
                ),
            )
            receipt("validation", ["execution/validation-repair-v1.json"], cycle)
            engine.transition(RunState.VALIDATING)
            repaired = _run_validation_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: run_validations(project_config.validation, effective_tier, worktree),
                repo.root,
                tuple(project_config.validation_links),
            )
            artifact = _save_validation(store, repaired, cycle)
            receipt(next_stage, [artifact], cycle)
            return repaired, not all_passed(repaired)

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
            store.write_text_artifact(
                stored_external, external_plan.read_text(encoding="utf-8"), immutable=True
            )
            store.update_manifest(external_plan_artifact=stored_external)
            external_plan = store.root / stored_external
        classifier_references = classification_references_for_run(
            store,
            project_config,
            request_text,
            fence,
            persist_if_missing=True,
        )
    except BaseException:
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
        stages_for(effective_tier)
        receipt(
            "planner"
            if effective_tier in {Tier.STANDARD, Tier.SUBSTANTIAL}
            else "implementation_worker",
            [
                "classification.json",
                "context.json",
                "request.md",
                "acceptance-criteria.json",
                "roles.json",
            ],
        )
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
        approved: str
        if effective_tier in {Tier.STANDARD, Tier.SUBSTANTIAL}:
            if external_plan is not None:
                version, digest = store.import_external_plan(external_plan)
                store.append_event(
                    {"event": "external_plan_selected", "version": version.name, "sha256": digest}
                )
            else:
                version = plan(
                    registry,
                    request_text,
                    invariants,
                    runtime.references,
                    store,
                )
            engine.transition(RunState.PLANNED)
            receipt("plan_reviewer", [f"planning/{version.name}"])
            plan_text = version.read_text(encoding="utf-8")
            plan_review = review_plan(
                registry,
                request_text,
                plan_text,
                runtime.profile_constraints,
                store,
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
            while plan_review.blocking_ids():
                if plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                    target = (
                        RunState.BLOCKED
                        if plan_review.outcome is Outcome.BLOCKED
                        else RunState.ESCALATED
                    )
                    _terminal(engine, target, "plan review: " + str(plan_review.outcome))
                    return _outcome(engine, repo=repo, worktree=worktree)
                reconciliation_cycles += 1
                if reconciliation_cycles > MAX_REMEDIATION_CYCLES:
                    raise RemediationExhausted(
                        f"plan findings {[f.id for f in plan_review.findings]} survived "
                        f"{MAX_REMEDIATION_CYCLES} reconciliation cycles",
                        cause="plan_review_exhausted",
                    )
                engine.transition(RunState.PLANNED)
                version = reconcile(registry, plan_text, plan_review.findings, store)
                receipt("plan_reviewer", [f"planning/{version.name}"], reconciliation_cycles)
                plan_text = version.read_text(encoding="utf-8")
                plan_review = review_plan(
                    registry,
                    request_text,
                    plan_text,
                    runtime.profile_constraints,
                    store,
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
            if plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                target = (
                    RunState.BLOCKED
                    if plan_review.outcome is Outcome.BLOCKED
                    else RunState.ESCALATED
                )
                _terminal(engine, target, "plan review: " + str(plan_review.outcome))
                return _outcome(engine, repo=repo, worktree=worktree)
            store.approve_plan(version)
            engine.transition(RunState.PLAN_FINALIZED)
            approved = store.approved_plan()
            approval_artifact = "planning/approved-plan.md"
        else:
            approved = _task_contract(request_text, project_config, acceptance_criteria, invariants)
            store.write_text_artifact("planning/task-contract.md", approved, immutable=True)
            store.update_manifest(
                plan_origin="task_contract", approved_plan_version="task-contract"
            )
            store.append_event({"event": "task_contract_written"})
            approval_artifact = "planning/task-contract.md"

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
            receipt("implementation_worker", [approval_artifact, "approval/human-approval.md"])
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

        receipt("implementation_worker", [approval_artifact])

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
            _run_worktree_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: execute(
                    registry,
                    approved,
                    invariants,
                    runtime.references,
                    worktree,
                    store,
                    runtime.profile_constraints,
                    scope_context,
                ),
            )
            receipt("validation", ["execution/worker-result.json"])

        engine.transition(RunState.VALIDATING)
        outcomes = _run_validation_stage(
            worktree_repo,
            base_commit,
            fence,
            lambda: run_validations(project_config.validation, effective_tier, worktree),
            repo.root,
            tuple(project_config.validation_links),
        )
        review_next = (
            "implementation_reviewer"
            if _implementation_review_required(effective_tier, project_config.validation)
            else "verifier"
        )
        _save_validation(store, outcomes)
        outcomes, repair_failed = repair_failed_validation(outcomes, 0, review_next)
        if repair_failed:
            _terminal(
                engine,
                RunState.FAILED,
                "required validation failed after repair",
                "validation_failed",
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
            )
            receipt(
                "remediation" if implementation_review.blocking_ids() else "verifier",
                [
                    f"review/{max((store.root / 'review').glob('implementation-review-v*.json')).name}",
                    "review/finding-index.json",
                ],
            )
            cleanup_ephemeral_paths(worktree_repo.root)
            if worktree_repo.change_inventory(base_commit) != review_inventory:
                raise ContextContractError("read-only implementation review changed the worktree")
        else:
            store.append_event(
                {
                    "event": "implementation_review_skipped",
                    "tier": str(effective_tier),
                    "reason": "tier stage path omits implementation review",
                }
            )
        cycle = 1
        while implementation_review is not None and implementation_review.blocking_ids():
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
                )
                return _outcome(engine, repo=repo, worktree=worktree)
            engine.transition(RunState.REMEDIATION)
            blocking = [
                finding
                for finding in implementation_review.findings
                if finding.id in implementation_review.blocking_ids()
            ]
            current_state = diff or "No committed or working-tree diff was observed."
            _run_worktree_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda blocking=blocking, current_state=current_state, cycle=cycle: remediate(
                    registry,
                    blocking,
                    approved,
                    current_state,
                    worktree,
                    store,
                    cycle=cycle,
                ),
            )
            receipt("validation", [f"execution/remediation-v{cycle}.json"], cycle)
            engine.transition(RunState.VALIDATING)
            outcomes = _run_validation_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: run_validations(project_config.validation, effective_tier, worktree),
                repo.root,
                tuple(project_config.validation_links),
            )
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
            )
            receipt(
                "remediation" if implementation_review.blocking_ids() else "verifier",
                [
                    f"review/{max((store.root / 'review').glob('implementation-review-v*.json')).name}",
                    "review/finding-index.json",
                ],
                cycle,
            )
            cleanup_ephemeral_paths(worktree_repo.root)
            if worktree_repo.change_inventory(base_commit) != review_inventory:
                raise ContextContractError("read-only implementation review changed the worktree")
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
            )
            return _outcome(engine, repo=repo, worktree=worktree)

        engine.transition(RunState.FINAL_VERIFICATION)
        verification = verify(
            registry,
            request_text,
            approved,
            acceptance_criteria,
            diff,
            _evidence(outcomes),
            (
                [finding.id for finding in implementation_review.findings]
                if implementation_review is not None
                else []
            ),
            store,
            worktree,
        )
        receipt("commit", ["verification/final-verification.json"])
        cleanup_ephemeral_paths(worktree_repo.root)
        if worktree_repo.change_inventory(base_commit) != review_inventory:
            raise ContextContractError("read-only verification changed the worktree")
        verdict_criteria = [verdict.criterion for verdict in verification.verdicts]
        all_verdicts_pass = all(verdict.verdict == "PASS" for verdict in verification.verdicts)
        criteria_bound = (
            list(verification.criteria) == list(acceptance_criteria)
            and verdict_criteria == list(acceptance_criteria)
            and all(verdict.evidence.strip() for verdict in verification.verdicts)
        )
        if (
            not criteria_bound
            or verification.outcome is not Outcome.PASS
            or not all_verdicts_pass
            or set(verification.unresolved_finding_ids)
            & set(implementation_review.blocking_ids() if implementation_review else [])
        ):
            _terminal(
                engine,
                RunState.FAILED,
                "final verification did not provide an unqualified PASS for the supplied criteria",
            )
            return _outcome(engine, verification, repo=repo, worktree=worktree)

        enforce_fence(worktree_repo, base_commit, fence)
        changed = worktree_repo.change_inventory(base_commit)
        if not changed:
            _terminal(engine, RunState.FAILED, "the run produced no change to commit")
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
    ) as exc:
        _terminal(
            engine,
            RunState.ESCALATED,
            str(exc),
            exc.cause if isinstance(exc, RemediationExhausted) else "other",
        )
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
    except (OSError, subprocess.SubprocessError) as exc:
        _terminal(engine, RunState.ESCALATED, f"provider invocation failed: {exc}")
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
