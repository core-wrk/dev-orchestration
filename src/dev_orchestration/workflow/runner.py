"""Drive one local run through the M2 workflow and its safety gates."""

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.adapters.registry import ReadOnlyRoleUnsupportedError, RoleRegistry
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.config.resolver import PROTECTED_DEFAULTS
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, RunState, Tier
from dev_orchestration.domain.findings import Verification
from dev_orchestration.domain.run import GitBlock, RoleBinding
from dev_orchestration.git.repo import EmptySnapshotError, GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    bootstrap_run,
    resolve_run_policy,
    resolve_runtime_context,
)
from dev_orchestration.workflow.engine import Engine, IllegalTransitionError
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    enforce_fence,
    enforce_ignored_writes,
)
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    FindingIdentityAmbiguity,
    RemediationExhausted,
    classify,
    execute,
    plan,
    reconcile,
    remediate,
    review_implementation,
    review_plan,
    verify,
)
from dev_orchestration.workflow.tiers import (
    TierDowngradeError,
    UnsupportedTierError,
    stages_for,
)
from dev_orchestration.workflow.validation import (
    ValidationOutcome,
    all_passed,
    run_validations,
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


def _record_roles(store, registry: RoleRegistry) -> None:
    roles = {}
    for role, binding in registry.roles.items():
        adapter = registry.adapters.get(binding.adapter)
        binary = getattr(adapter, "binary", None)
        roles[role] = RoleBinding(
            adapter=binding.adapter,
            model_alias=binding.model,
            reasoning=binding.reasoning,
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
) -> None:
    store = engine.store
    store.update_manifest(terminal_reason=reason)
    store.append_event({"event": "run_terminal", "state": str(state), "detail": reason})
    if engine.state not in {
        RunState.COMPLETE_LOCAL,
        RunState.ESCALATED,
        RunState.FAILED,
        RunState.CANCELLED,
    }:
        engine.transition(state)


def _outcome(engine: Engine, verification: Verification | None = None) -> RunOutcome:
    manifest = engine.store.read_manifest()
    return RunOutcome(
        run_id=manifest.run_id,
        final_state=manifest.status,
        store_root=engine.store.root,
        verification=verification,
        reason=manifest.terminal_reason or "",
    )


def _effective_tier(
    classification_tier: Tier,
    override: Tier | None,
    minimum: Tier | None,
) -> Tier:
    requested = classification_tier
    if override is not None:
        if _tier_rank(override) < _tier_rank(classification_tier):
            raise TierDowngradeError(
                f"--tier {override} is below the classified tier {classification_tier}; "
                "an override may raise a run's controls but never lower them"
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
        worktree_repo.restore_ignored_snapshot(ignored_before)
        raise
    worktree_repo.restore_ignored_snapshot(ignored_before)
    return result


def _run_validation_stage(worktree_repo: GitRepo, base_commit: str, fence: ScopeFence, action):
    """Run validation while ensuring its ignored caches do not become residue."""
    ignored_before = worktree_repo.ignored_paths()
    try:
        result = action()
        enforce_fence(worktree_repo, base_commit, fence)
        return result
    finally:
        worktree_repo.restore_ignored_snapshot(ignored_before)


def execute_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    request_text: str,
    worktree_root: Path,
    acceptance_criteria: list[str],
    *,
    tier_override: Tier | None = None,
    external_plan: Path | None = None,
    acceptance_criteria_source: str = "explicit",
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
    store, worktree = bootstrap_run(
        repo, project_config, request_text, Tier.STANDARD, worktree_root
    )
    engine = Engine(store)
    fence = ScopeFence.from_config(project_config.scope)
    base_commit = store.read_manifest().git.base_commit
    if base_commit is None:
        _terminal(engine, RunState.FAILED, "bootstrap did not record a base commit")
        return _outcome(engine)
    worktree_repo = GitRepo(worktree)
    store.update_manifest(
        acceptance_criteria=acceptance_criteria,
        acceptance_criteria_source=acceptance_criteria_source,
        tier_override=tier_override,
    )
    _record_roles(store, registry)
    store.update_manifest(
        available_profiles=project_config.profiles.available,
        config_layers=[project_config.model_dump(mode="json")],
    )
    store.write_text_artifact("request.md", request_text + "\n", immutable=True)
    store.write_json_artifact("acceptance-criteria.json", acceptance_criteria, immutable=True)

    try:
        classification = classify(
            registry,
            request_text,
            project_config.project.project_class,
            store,
            project_config.profiles.available,
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
        effective_tier = _effective_tier(classification.tier, tier_override, runtime.minimum_tier)
        store.update_manifest(tier=effective_tier)
        stages_for(effective_tier)
        if external_plan is not None and effective_tier is not Tier.STANDARD:
            raise ContextContractError("an external plan requires the standard tier")
        if effective_tier is not classification.tier:
            store.append_event(
                {
                    "event": "tier_resolved",
                    "classified": str(classification.tier),
                    "effective": str(effective_tier),
                    "override": str(tier_override) if tier_override else None,
                    "minimum": str(runtime.minimum_tier) if runtime.minimum_tier else None,
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
        if effective_tier is Tier.STANDARD:
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
            plan_text = version.read_text(encoding="utf-8")
            plan_review = review_plan(
                registry,
                request_text,
                plan_text,
                runtime.profile_constraints,
                store,
            )
            engine.transition(RunState.PLAN_REVIEWED)
            reconciliation_cycles = 0
            while plan_review.blocking_ids():
                if plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                    target = (
                        RunState.BLOCKED
                        if plan_review.outcome is Outcome.BLOCKED
                        else RunState.ESCALATED
                    )
                    _terminal(engine, target, "plan review: " + str(plan_review.outcome))
                    return _outcome(engine)
                reconciliation_cycles += 1
                if reconciliation_cycles > MAX_REMEDIATION_CYCLES:
                    raise RemediationExhausted(
                        f"plan findings {[f.id for f in plan_review.findings]} survived "
                        f"{MAX_REMEDIATION_CYCLES} reconciliation cycles"
                    )
                engine.transition(RunState.PLANNED)
                version = reconcile(registry, plan_text, plan_review.findings, store)
                plan_text = version.read_text(encoding="utf-8")
                plan_review = review_plan(
                    registry,
                    request_text,
                    plan_text,
                    runtime.profile_constraints,
                    store,
                )
                engine.transition(RunState.PLAN_REVIEWED)
            if plan_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                target = (
                    RunState.BLOCKED
                    if plan_review.outcome is Outcome.BLOCKED
                    else RunState.ESCALATED
                )
                _terminal(engine, target, "plan review: " + str(plan_review.outcome))
                return _outcome(engine)
            store.approve_plan(version)
            engine.transition(RunState.PLAN_FINALIZED)
            approved = store.approved_plan()
        else:
            approved = _task_contract(request_text, project_config, acceptance_criteria, invariants)
            store.write_text_artifact("planning/task-contract.md", approved, immutable=True)
            store.update_manifest(
                plan_origin="task_contract", approved_plan_version="task-contract"
            )
            store.append_event({"event": "task_contract_written"})

        engine.transition(RunState.EXECUTING)
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

        engine.transition(RunState.VALIDATING)
        outcomes = _run_validation_stage(
            worktree_repo,
            base_commit,
            fence,
            lambda: run_validations(project_config.validation, effective_tier, worktree),
        )
        store.write_json_artifact(
            "execution/validation-v1.json", _validation_json(outcomes), immutable=True
        )
        store.append_event(
            {
                "event": "validated",
                "passed": all_passed(outcomes),
                "required_commands": [outcome.name for outcome in outcomes],
                "detail": (
                    "No required validation commands were resolved for this tier."
                    if not outcomes
                    else ""
                ),
                "outcomes": _validation_json(outcomes),
            }
        )
        if not all_passed(outcomes):
            _terminal(engine, RunState.FAILED, "required validation failed")
            return _outcome(engine)

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
        if worktree_repo.change_inventory(base_commit) != review_inventory:
            raise ContextContractError("read-only implementation review changed the worktree")
        cycle = 1
        while implementation_review.blocking_ids():
            if implementation_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
                target = (
                    RunState.BLOCKED
                    if implementation_review.outcome is Outcome.BLOCKED
                    else RunState.ESCALATED
                )
                _terminal(
                    engine, target, "implementation review: " + str(implementation_review.outcome)
                )
                return _outcome(engine)
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
            engine.transition(RunState.VALIDATING)
            outcomes = _run_validation_stage(
                worktree_repo,
                base_commit,
                fence,
                lambda: run_validations(project_config.validation, effective_tier, worktree),
            )
            store.write_json_artifact(
                f"execution/validation-v{cycle + 1}.json",
                _validation_json(outcomes),
                immutable=True,
            )
            store.append_event(
                {
                    "event": "validated",
                    "cycle": cycle,
                    "passed": all_passed(outcomes),
                    "outcomes": _validation_json(outcomes),
                }
            )
            if not all_passed(outcomes):
                _terminal(engine, RunState.FAILED, "required validation failed after remediation")
                return _outcome(engine)
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
            if worktree_repo.change_inventory(base_commit) != review_inventory:
                raise ContextContractError("read-only implementation review changed the worktree")
            cycle += 1

        if implementation_review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}:
            target = (
                RunState.BLOCKED
                if implementation_review.outcome is Outcome.BLOCKED
                else RunState.ESCALATED
            )
            _terminal(
                engine, target, "implementation review: " + str(implementation_review.outcome)
            )
            return _outcome(engine)

        engine.transition(RunState.FINAL_VERIFICATION)
        verification = verify(
            registry,
            request_text,
            approved,
            acceptance_criteria,
            diff,
            _evidence(outcomes),
            [finding.id for finding in implementation_review.findings],
            store,
        )
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
            or set(verification.unresolved_finding_ids) & set(implementation_review.blocking_ids())
        ):
            _terminal(
                engine,
                RunState.FAILED,
                "final verification did not provide an unqualified PASS for the supplied criteria",
            )
            return _outcome(engine, verification)

        enforce_fence(worktree_repo, base_commit, fence)
        changed = worktree_repo.change_inventory(base_commit)
        if not changed:
            _terminal(engine, RunState.FAILED, "the run produced no change to commit")
            return _outcome(engine, verification)
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
        return _outcome(engine, verification)

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
        _terminal(engine, RunState.ESCALATED, str(exc))
        return _outcome(engine)
    except (IllegalTransitionError, UnsupportedTierError, TierDowngradeError) as exc:
        _terminal(engine, RunState.ESCALATED, str(exc))
        return _outcome(engine)
    except (OSError, subprocess.SubprocessError) as exc:
        _terminal(engine, RunState.ESCALATED, f"provider invocation failed: {exc}")
        return _outcome(engine)
    except Exception as exc:  # noqa: BLE001 - guarantee a durable terminal state
        _terminal(engine, RunState.FAILED, f"unexpected workflow failure: {exc}")
        return _outcome(engine)


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
