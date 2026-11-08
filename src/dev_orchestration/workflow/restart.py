"""Start a linked post-implementation recovery attempt from immutable run evidence."""

import hashlib
import json
import tempfile
from pathlib import Path

from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.domain.enums import Outcome, RunState
from dev_orchestration.domain.findings import Classification, ReviewResult
from dev_orchestration.git.repo import GitCommandError, GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    bootstrap_run,
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
from dev_orchestration.workflow.recovery import ResumeRefused, _verify_human_approval, resume_run
from dev_orchestration.workflow.runner import RunOutcome, cleanup_worktree_if_unproductive
from dev_orchestration.workflow.scope_check import ScopeViolation, enforce_fence
from dev_orchestration.workflow.snapshot import capture_snapshot
from dev_orchestration.workflow.stages import discover_dependencies
from dev_orchestration.workflow.validation import (
    ValidationEnvironmentError,
    ValidationOutcome,
    all_passed,
    validation_file_hashes,
)

_FINAL_STATES = {RunState.FAILED, RunState.ESCALATED}
_GOVERNANCE_CAUSES = {
    "implementation_review_escalated",
    "plan_review_exhausted",
    "scope_violation",
    "safety_escalation",
    "restart_governance_blocked",
}


def _code_digest(snapshot: dict) -> str:
    """Fingerprint changed paths independently of worktree and branch names."""
    payload = {"base": snapshot["base"], "entries": snapshot["entries"]}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _patch_code_digest(repo: GitRepo, base_commit: str, patch: bytes) -> str:
    """Compute the normal source snapshot fingerprint from an archived patch."""
    with tempfile.TemporaryDirectory(prefix="dev-orch-restart-") as scratch:
        worktree = Path(scratch) / "checkout"
        repo.run_git("worktree", "add", "--quiet", "--detach", str(worktree), base_commit)
        try:
            checkout = GitRepo(worktree)
            checkout.apply_patch_bytes(patch)
            return _code_digest(capture_snapshot(checkout, base_commit))
        finally:
            if worktree.exists():
                from dev_orchestration.git.worktree import remove_worktree

                remove_worktree(repo, worktree)


def _patch_paths(repo: GitRepo, base_commit: str, patch: bytes) -> set[str]:
    """Return every path touched by a patch, including both sides of renames."""
    with tempfile.TemporaryDirectory(prefix="dev-orch-restart-paths-") as scratch:
        worktree = Path(scratch) / "checkout"
        repo.run_git("worktree", "add", "--quiet", "--detach", str(worktree), base_commit)
        try:
            checkout = GitRepo(worktree)
            checkout.apply_patch_bytes(patch)
            return set(checkout.change_inventory(base_commit))
        finally:
            if worktree.exists():
                from dev_orchestration.git.worktree import remove_worktree

                remove_worktree(repo, worktree)


def _require_compatible_base(repo: GitRepo, source_base: str, patch: bytes) -> str:
    destination_base = repo.current_commit()
    if destination_base == source_base:
        return destination_base
    if not repo.is_ancestor(source_base, destination_base):
        raise ResumeRefused("repository base diverged; start a new run against the current base")
    changed_paths = set(repo.change_inventory(source_base))
    patch_paths = _patch_paths(repo, source_base, patch)
    overlap = sorted(changed_paths & patch_paths)
    if overlap:
        raise ResumeRefused(
            "repository base changed paths touched by the saved patch: " + ", ".join(overlap)
        )
    return destination_base


def _destination_patch_digest(repo: GitRepo, base_commit: str, patch: bytes) -> str:
    try:
        return _patch_code_digest(repo, base_commit, patch)
    except GitCommandError as exc:
        raise ResumeRefused(
            f"saved patch does not apply cleanly to the current base: {exc}"
        ) from exc


def _version(path: str) -> int:
    try:
        return int(Path(path).stem.rsplit("-v", 1)[1])
    except (IndexError, ValueError):
        return -1


def _validation_command_changes(saved_config: dict, current_config: ProjectConfig) -> dict | None:
    """Return changed validation commands only, or None for any broader edit."""
    try:
        old = ProjectConfig.model_validate(saved_config).model_dump(mode="json")
    except ValueError:
        return None
    new = current_config.model_dump(mode="json")
    old_validation = old.pop("validation", {})
    new_validation = new.pop("validation", {})
    if old != new or set(old_validation) != set(new_validation):
        return None
    changes = {}
    for name in old_validation:
        old_gate = dict(old_validation[name])
        new_gate = dict(new_validation[name])
        old_command = old_gate.pop("command", None)
        new_command = new_gate.pop("command", None)
        if old_gate != new_gate:
            return None
        if old_command != new_command:
            changes[name] = {"from": old_command, "to": new_command}
    return changes or None


def _receipt_for(store: RunStore, *, next_stage: str) -> dict | None:
    receipts = []
    for path in (store.root / "checkpoints").glob("*.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if receipt.get("next_stage") == next_stage:
            receipts.append(receipt)
    return max(receipts, key=lambda item: item.get("sequence", 0), default=None)


def _source_contract(
    repo: GitRepo,
    config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    *,
    allow_validation_command_changes: bool = False,
) -> tuple[RunStore, dict, Path | None]:
    source = RunStore(repo.root, run_id)
    if source.root.resolve().parent != RunStore.runs_directory(repo.root).resolve():
        raise ResumeRefused("run ID must name a run in this repository")
    if not (source.root / "manifest.json").is_file():
        raise ResumeRefused(f"run {run_id!r} does not exist")
    manifest = source.read_manifest()
    if manifest.status not in _FINAL_STATES:
        raise ResumeRefused(f"run is {manifest.status}; restart accepts failed or escalated runs")
    if manifest.terminal_cause in _GOVERNANCE_CAUSES:
        raise ResumeRefused(
            f"run ended with governance cause {manifest.terminal_cause!r}; resolve it in a new run"
        )
    if manifest.cloud_session is not None:
        raise ResumeRefused("cloud-linked runs cannot use local restart")
    if not manifest.request_text or not manifest.request_text.strip():
        raise ResumeRefused("source request is missing")
    if not manifest.git.base_commit or not manifest.git.branch:
        raise ResumeRefused("source run has no recorded base and branch")
    if not repo.is_ancestor(manifest.git.base_commit, repo.current_commit()):
        raise ResumeRefused("repository base diverged; start a new run against the current base")
    if not manifest.config_layers:
        raise ResumeRefused("source run has no saved project configuration; restart is unavailable")
    validation_changes = None
    if not project_config_matches(manifest.config_layers[0], config):
        if allow_validation_command_changes:
            validation_changes = _validation_command_changes(manifest.config_layers[0], config)
        if validation_changes is None:
            raise ResumeRefused("project configuration changed since the source run")
    if resolve_run_policy(manifest.config_layers) != manifest.resolved_config:
        raise ResumeRefused("protected policy changed since the source run")
    if source.active_provider_alive():
        raise ResumeRefused("source run still has an active provider")
    if manifest.checkpoint is None:
        raise ResumeRefused("source run has no durable stage evidence; restart is ambiguous")
    if config.validation_files and not manifest.validation_input_hashes:
        raise ResumeRefused("saved validation input hashes are missing")
    if manifest.validation_input_hashes:
        try:
            current_validation_hashes = validation_file_hashes(repo, config.validation_files)
        except ValidationEnvironmentError as exc:
            raise ResumeRefused(
                f"declared validation inputs are unavailable; restore the recorded bytes for "
                f"recovery, or start a fresh run for intentionally different inputs: {exc}"
            ) from exc
        if current_validation_hashes != manifest.validation_input_hashes:
            raise ResumeRefused(
                "declared validation inputs changed since the source run; restore the recorded "
                "bytes for recovery, or start a fresh run for intentionally different inputs"
            )
    try:
        latest = source.verify_checkpoint()
        current_inputs = input_hashes(
            source,
            registry,
            allow_changed_dependency_sources=config.context.dependency_discovery,
        )
        saved_inputs = latest["inputs"]
        if "validation_input_hashes" not in saved_inputs:
            current_inputs.pop("validation_input_hashes", None)
        if validation_changes is not None:
            current_inputs.pop(".ai/project.yaml", None)
            saved_inputs = dict(saved_inputs)
            saved_inputs.pop(".ai/project.yaml", None)
        if config.context.dependency_discovery:
            current_inputs = {
                key: value
                for key, value in current_inputs.items()
                if key != "dependency-report.json"
                and not key.startswith("dependency_source:")
                and key != "dependency_inventory"
            }
            saved_inputs = {
                key: value
                for key, value in saved_inputs.items()
                if key != "dependency-report.json"
                and not key.startswith("dependency_source:")
                and key != "dependency_inventory"
            }
        if (
            ".ai/project.yaml" in current_inputs
            and ".ai/project.yaml" in saved_inputs
            and current_inputs[".ai/project.yaml"] != saved_inputs[".ai/project.yaml"]
            and project_config_matches(manifest.config_layers[0], config)
            and (
                dependency_discovery_default_omission(manifest.config_layers[0], config)
                or validation_files_default_omission(repo, manifest.git.base_commit or "")
            )
        ):
            current_inputs[".ai/project.yaml"] = saved_inputs[".ai/project.yaml"]
        if current_inputs != saved_inputs:
            raise ResumeRefused("saved request, policy, role binding, or declared context changed")
    except CheckpointError as exc:
        raise ResumeRefused(str(exc)) from exc
    for name in ("request.md", "acceptance-criteria.json", "roles.json", "classification.json"):
        if not (source.root / name).is_file():
            raise ResumeRefused(f"source run is missing {name}")
    if (source.root / "request.md").read_text(encoding="utf-8").rstrip(
        "\n"
    ) != manifest.request_text:
        raise ResumeRefused("saved request artifact does not match the run manifest")
    if (
        json.loads((source.root / "acceptance-criteria.json").read_text())
        != manifest.acceptance_criteria
    ):
        raise ResumeRefused("saved acceptance criteria do not match the run manifest")
    classification = Classification.model_validate_json(
        (source.root / "classification.json").read_text(encoding="utf-8")
    )
    if classification.tier != manifest.tier or classification.profiles != manifest.active_profiles:
        raise ResumeRefused("saved classification does not match the run policy")
    plan_relative = (
        "planning/task-contract.md"
        if manifest.plan_origin == "task_contract"
        else "planning/approved-plan.md"
    )
    plan = source.root / plan_relative
    if not plan.is_file():
        raise ResumeRefused("approved plan or task contract is missing")
    plan_digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    if manifest.plan_sha256 and manifest.plan_sha256 != plan_digest:
        raise ResumeRefused("approved plan changed since the source run")
    if manifest.approval_required:
        _verify_human_approval(source)
    fence = ScopeFence.from_config(config.scope)
    runtime = resolve_runtime_context(repo.root, config, manifest.active_profiles, fence)
    if (
        runtime.included != manifest.context_included
        or runtime.excluded != manifest.context_excluded
        or runtime.conflicts != manifest.context_conflicts
        or runtime.minimum_tier != manifest.minimum_tier
    ):
        raise ResumeRefused("declared context changed since the source run")
    if any(
        event.get("event") in {"scope_violation", "scope_check_failed"} for event in _events(source)
    ):
        raise ResumeRefused("source history contains a scope escalation")
    reason = (manifest.terminal_reason or "").lower()
    if any(
        word in reason
        for word in (
            "scope violation",
            "safety escalation",
            "implementation review: blocked",
            "implementation review: escalate",
            "plan review: blocked",
            "plan review: escalate",
        )
    ):
        raise ResumeRefused("source terminal reason identifies an unresolved governance blocker")
    if manifest.terminal_cause == "other":
        # Cause `other` is useful only when the durable record proves execution reached
        # implementation. Ambiguous pre-implementation failures stay out of scope.
        proven = any(
            name in latest["artifacts"]
            for name in ("execution/worker-result.json", "execution/validation-v1.json")
        ) or any((source.root / "execution").glob("validation-v*.json"))
        if not proven:
            raise ResumeRefused("cause 'other' has no evidence that implementation completed")
    worktree = Path(manifest.git.worktree) if manifest.git.worktree else None
    if worktree is not None and worktree.is_dir():
        tree = GitRepo(worktree)
        if (
            tree.current_branch() != manifest.git.branch
            or tree.current_commit() != manifest.git.base_commit
        ):
            raise ResumeRefused("source worktree branch or base changed")
        try:
            enforce_fence(tree, manifest.git.base_commit, fence)
        except ScopeViolation as exc:
            raise ResumeRefused(f"source implementation violates scope: {exc}") from exc
    elif not (source.root / "execution/final-change.patch").is_file():
        raise ResumeRefused("source worktree and saved implementation patch are both missing")
    else:
        worktree = None
    return source, latest, worktree


def _events(store: RunStore) -> list[dict]:
    path = store.root / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _validation_evidence(source: RunStore, code_digest: str) -> tuple[str, list[dict]]:
    receipt = _receipt_for(source, next_stage="implementation_reviewer")
    if receipt is None:
        receipt = _receipt_for(source, next_stage="verifier")
    if receipt is None or _code_digest(receipt["worktree"]) != code_digest:
        raise ResumeRefused(
            "review shortcut requires passing validation for this exact diff; use --from validation"
        )
    names = [
        name
        for name in receipt["artifacts"]
        if name.startswith("execution/validation-") and name.endswith(".json")
    ]
    if not names:
        raise ResumeRefused("matching validation artifact is missing; use --from validation")
    name = max(names, key=_version)
    outcomes = [
        ValidationOutcome.model_validate(item)
        for item in json.loads((source.root / name).read_text(encoding="utf-8"))
    ]
    if not all_passed(outcomes):
        raise ResumeRefused("saved validation did not pass; use --from validation")
    return name, outcomes


def _review_evidence(source: RunStore, code_digest: str) -> tuple[str, str, str]:
    receipt = _receipt_for(source, next_stage="verifier")
    if receipt is None or _code_digest(receipt["worktree"]) != code_digest:
        raise ResumeRefused(
            "verification shortcut requires review for this exact diff; use --from validation"
        )
    reviews = [
        name
        for name in receipt["artifacts"]
        if name.startswith("review/implementation-review-") and name.endswith(".json")
    ]
    validation = [
        name
        for name in receipt["artifacts"]
        if name.startswith("execution/validation-") and name.endswith(".json")
    ]
    if not reviews or not validation:
        raise ResumeRefused(
            "matching review or validation evidence is missing; use --from validation"
        )
    manifest = source.read_manifest()
    if (
        manifest.config_layers
        and ProjectConfig.model_validate(manifest.config_layers[0]).context.dependency_discovery
    ):
        report_path = source.root / "dependency-report.json"
        identity = None
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            identity = {
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "source_inventory_identity": report.get("source_inventory_identity"),
            }
        pinned = receipt.get("inputs", {})
        if (
            identity is None
            or "dependency-report.json" not in receipt.get("artifacts", [])
            or pinned.get("dependency-report.json") != identity["report_sha256"]
            or pinned.get("dependency_inventory") != identity["source_inventory_identity"]
            or not identity["source_inventory_identity"]
        ):
            raise ResumeRefused(
                "saved implementation review does not pin dependency evidence; "
                "use --from validation"
            )
    review_name = max(reviews, key=_version)
    validation_name = max(validation, key=_version)
    reviewed = ReviewResult.model_validate_json((source.root / review_name).read_text())
    outcomes = [
        ValidationOutcome.model_validate(item)
        for item in json.loads((source.root / validation_name).read_text())
    ]
    if reviewed.outcome in {Outcome.BLOCKED, Outcome.ESCALATE} or reviewed.blocking_ids():
        raise ResumeRefused(
            "saved implementation review has unresolved blockers; use --from validation"
        )
    if not all_passed(outcomes):
        raise ResumeRefused("saved validation did not pass; use --from validation")
    return review_name, validation_name, "review/finding-index.json"


def _verification_evidence(
    source: RunStore, code_digest: str, *, review_required: bool
) -> tuple[str, str | None, str | None]:
    if review_required:
        review, validation, index = _review_evidence(source, code_digest)
        return validation, review, index
    validation, _ = _validation_evidence(source, code_digest)
    return validation, None, None


def _review_matches_dependency_identity(source: RunStore, current: dict) -> bool:
    manifest = source.read_manifest()
    if (
        not manifest.config_layers
        or not ProjectConfig.model_validate(manifest.config_layers[0]).context.dependency_discovery
    ):
        return True
    receipt = _receipt_for(source, next_stage="verifier")
    path = source.root / "dependency-report.json"
    if receipt is None or not path.is_file():
        return False
    artifacts = receipt.get("artifacts", [])
    if not any(
        name.startswith("review/implementation-review-") and name.endswith(".json")
        for name in artifacts
    ):
        return False
    report = json.loads(path.read_text(encoding="utf-8"))
    saved = {
        "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_inventory_identity": report.get("source_inventory_identity"),
    }
    inputs = receipt.get("inputs", {})
    return (
        saved == current
        and "dependency-report.json" in artifacts
        and inputs.get("dependency-report.json") == saved["report_sha256"]
        and inputs.get("dependency_inventory") == saved["source_inventory_identity"]
        and bool(saved["source_inventory_identity"])
    )


def restart_status(
    repo: GitRepo, config: ProjectConfig, registry: RoleRegistry, run_id: str
) -> str:
    """Describe safe restart stages without invoking an agent."""
    try:
        source, _, worktree = _source_contract(
            repo, config, registry, run_id, allow_validation_command_changes=True
        )
        manifest = source.read_manifest()
        saved_config = ProjectConfig.model_validate(manifest.config_layers[0])
        validation_commands_changed = saved_config.model_dump(mode="json") != config.model_dump(
            mode="json"
        )
        if worktree is None:
            patch = (source.root / "execution/final-change.patch").read_bytes()
            base = _require_compatible_base(repo, manifest.git.base_commit or "", patch)
            digest = _destination_patch_digest(repo, base, patch)
        else:
            patch = GitRepo(worktree).change_patch_bytes(manifest.git.base_commit)
            base = _require_compatible_base(repo, manifest.git.base_commit or "", patch)
            digest = _destination_patch_digest(repo, base, patch)
        stages = ["validation"]
        blockers = []
        if validation_commands_changed:
            detail = (
                "Restart: eligible --from validation; review, verification, and remediation "
                "require unchanged configuration"
            )
            return detail
        try:
            _validation_evidence(source, digest)
            if manifest.tier.value in {"standard", "substantial"}:
                stages.append("review")
            try:
                _verification_evidence(
                    source,
                    digest,
                    review_required=manifest.tier.value in {"standard", "substantial"},
                )
                stages.append("verification")
            except ResumeRefused as exc:
                if config.context.dependency_discovery and "dependency evidence" in str(exc):
                    stages.append("verification after fresh review")
                    blockers.append(
                        "changed dependency evidence requires implementation review "
                        "before verification"
                    )
                else:
                    blockers.append(str(exc))
        except ResumeRefused as exc:
            blockers.append(str(exc))
        stages.append("remediation")
        detail = f"Restart: eligible --from {'|'.join(stages)}"
        if config.context.dependency_discovery and manifest.tier.value in {
            "standard",
            "substantial",
        }:
            detail += (
                "; verification restart checks fresh dependency evidence and reviews again "
                "if it changed"
            )
        if blockers:
            detail += "; shortcuts refused: " + " / ".join(dict.fromkeys(blockers))
        return detail
    except (ResumeRefused, GitCommandError, OSError, ValueError, KeyError) as exc:
        return f"Restart: unavailable: {exc}"


def restart_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    registry: RoleRegistry,
    run_id: str,
    worktree_root: Path,
    *,
    from_stage: str = "validation",
    reason: str,
    patch_path: Path | None = None,
) -> RunOutcome:
    """Create a linked attempt that rechecks an existing implementation."""
    if from_stage not in {"validation", "review", "verification", "remediation"}:
        raise ResumeRefused("--from must be validation, review, verification, or remediation")
    if not reason.strip():
        raise ResumeRefused("restart requires a non-empty --reason")
    source, _, source_worktree = _source_contract(
        repo,
        project_config,
        registry,
        run_id,
        allow_validation_command_changes=from_stage == "validation",
    )
    original = source.read_manifest()
    validation_command_changes = (
        _validation_command_changes(original.config_layers[0], project_config)
        if original.config_layers
        else None
    )
    if not original.goal_id:
        raise ResumeRefused(
            f"legacy run {run_id} has no goal ID; associate it with `dev-orch goal-associate` first"
        )
    if from_stage in {"review", "verification"} and original.tier.value not in {
        "standard",
        "substantial",
    }:
        raise ResumeRefused(f"tier {original.tier} does not include implementation review")
    fence = ScopeFence.from_config(project_config.scope)
    if patch_path is not None:
        patch_bytes = patch_path.expanduser().resolve().read_bytes()
    elif source_worktree is not None:
        patch_bytes = GitRepo(source_worktree).change_patch_bytes(original.git.base_commit)
    else:
        patch_bytes = (source.root / "execution/final-change.patch").read_bytes()
    if not patch_bytes.strip():
        raise ResumeRefused("source has no implementation changes to restart")

    source_base = original.git.base_commit or ""
    destination_base = _require_compatible_base(repo, source_base, patch_bytes)
    base_changed = destination_base != source_base
    destination_digest = _destination_patch_digest(repo, destination_base, patch_bytes)
    if base_changed and from_stage in {"review", "verification"}:
        raise ResumeRefused(
            "base changed; review and verification shortcuts require fresh validation"
        )

    source_digest = _patch_code_digest(repo, source_base, patch_bytes)
    if from_stage == "review":
        _validation_evidence(source, source_digest)
    elif from_stage == "verification":
        if project_config.context.dependency_discovery:
            _validation_evidence(source, source_digest)
        else:
            _verification_evidence(
                source,
                source_digest,
                review_required=original.tier.value in {"standard", "substantial"},
            )

    # A reviewer/verifier shortcut is eligible only when source evidence pins this
    # patch to the exact checked implementation. The patch is applied below before
    # the final fingerprint is checked again.
    requested_next = {
        "validation": "validation",
        "review": "implementation_reviewer",
        "verification": "verifier",
        "remediation": "validation",
    }[from_stage]
    config = project_config
    store, worktree = bootstrap_run(
        repo,
        config,
        original.request_text or "",
        original.tier,
        worktree_root,
        original.goal_id,
        manual_start=True,
        supplied_edit=patch_path is not None,
    )
    try:
        worktree_repo = GitRepo(worktree)
        worktree_repo.apply_patch_bytes(patch_bytes)
        enforce_fence(worktree_repo, destination_base, fence)
        snapshot = capture_snapshot(worktree_repo, destination_base)
        code_digest = _code_digest(snapshot)
        if code_digest != destination_digest:
            raise ResumeRefused("archived patch changed while preparing the restart")
        if not base_changed and code_digest != source_digest:
            raise ResumeRefused("archived patch changed while preparing the restart")
        if from_stage == "review":
            _validation_evidence(source, code_digest)
        elif from_stage == "verification":
            if project_config.context.dependency_discovery:
                _validation_evidence(source, code_digest)
            else:
                _verification_evidence(
                    source,
                    code_digest,
                    review_required=original.tier.value in {"standard", "substantial"},
                )
        mode = "remediate" if from_stage == "remediation" else "check_only"

        # Copy immutable request and approved-contract inputs; keep review outputs in
        # a historical folder unless they are the exact evidence authorizing a shortcut.
        manifest_data = original.model_dump(mode="python")
        manifest_data.update(
            {
                "run_id": store.run_id,
                "repository": str(repo.root),
                "git": store.read_manifest().git,
                "goal_id": original.goal_id,
                "epic_id": original.goal_id,
                "goal_attempt": store.read_manifest().goal_attempt,
                "goal_generation": store.read_manifest().goal_generation,
                "status": RunState.VALIDATING,
                "created_at": store.read_manifest().created_at,
                "completed_at": None,
                "terminal_reason": None,
                "terminal_cause": None,
                "failure_class": None,
                "failure_reason": None,
                "failure_signature": None,
                "retry_of": None,
                "checkpoint": None,
                "auto_resume": False,
                "pause": None,
                "cloud_session": None,
                "linked_source_run": run_id,
                "restart_mode": mode,
                "restart_from": from_stage,
                "restart_reason": reason.strip(),
                "restart_code_sha256": code_digest,
                "restart_source_base_commit": source_base,
                "restart_destination_base_commit": destination_base,
                "stage_attempts": {},
                "stage_actor": None,
                "stage_error": None,
                "stop": None,
                "repair_rounds": {},
                "validation_repairs_reserved": 0,
                "validation_artifact": None,
                "roles": original.roles,
                "config_layers": [
                    project_config.model_dump(mode="json"),
                    *original.config_layers[1:],
                ],
                "resolved_config": resolve_run_policy(
                    [project_config.model_dump(mode="json"), *original.config_layers[1:]]
                ),
            }
        )
        store.update_manifest(**manifest_data)
        store.append_event(
            {
                "event": "restart_created",
                "source_run": run_id,
                "requested_stage": from_stage,
                "mode": mode,
                "reason": reason.strip(),
                "code_sha256": code_digest,
                "source_base_commit": source_base,
                "destination_base_commit": destination_base,
            }
        )
        if validation_command_changes is not None:
            store.append_event(
                {"event": "validation_commands_changed", "commands": validation_command_changes}
            )

        def copy_artifact(source_relative: str, target_relative: str | None = None) -> str:
            target = target_relative or source_relative
            source_root = source.root.resolve()
            target_root = store.root.resolve()
            path = (source.root / source_relative).resolve()
            target_path = (store.root / target).resolve()
            if not path.is_relative_to(source_root) or not target_path.is_relative_to(target_root):
                raise ResumeRefused("saved artifact path escaped its run directory")
            if not path.is_file():
                raise ResumeRefused(f"source artifact {source_relative} is missing")
            store.write_bytes_artifact(target, path.read_bytes(), immutable=True)
            return target

        copy_artifact("request.md")
        copy_artifact("acceptance-criteria.json")
        copy_artifact("classification.json")
        copy_artifact("context.json")
        if (source.root / "classification-inputs.json").is_file():
            copy_artifact("classification-inputs.json")
        store.write_json_artifact(
            "roles.json",
            {name: value.model_dump(mode="json") for name, value in original.roles.items()},
            immutable=True,
        )
        contract = (
            "planning/task-contract.md"
            if original.plan_origin == "task_contract"
            else "planning/approved-plan.md"
        )
        contract_text = (source.root / contract).read_bytes()
        store.write_bytes_artifact(contract, contract_text, immutable=True)
        if contract.endswith("approved-plan.md"):
            store.write_bytes_artifact("planning/plan-v1.md", contract_text, immutable=True)
        if original.external_plan_artifact:
            copy_artifact(original.external_plan_artifact)
        if original.seed_patch_artifact:
            copy_artifact(original.seed_patch_artifact)
            copy_artifact("execution/seed-patch-receipt.json")
        for history_path in (source.root / "review").glob("*.json"):
            if history_path.is_file():
                relative = history_path.relative_to(source.root).as_posix()
                if relative == "review/finding-index.json":
                    copy_artifact(relative, "history/source-finding-index.json")
                elif relative.startswith("review/implementation-review-"):
                    copy_artifact(relative, f"history/source/{relative}")
        if original.approval_required:
            copy_artifact("approval/human-approval.md")
            copy_artifact("approval/approved.json")

        if project_config.context.dependency_discovery:
            inventory, source_metadata = dependency_sources(repo.root, project_config)
            discover_dependencies(
                registry,
                original.request_text or "",
                inventory,
                source_metadata,
                resolve_runtime_context(
                    repo.root, project_config, original.active_profiles, fence
                ).invariants,
                store,
            )
            report_path = store.root / "dependency-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            fresh_dependency_identity = {
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "source_inventory_identity": report.get("source_inventory_identity"),
            }
            if from_stage == "verification" and not _review_matches_dependency_identity(
                source, fresh_dependency_identity
            ):
                requested_next = "implementation_reviewer"
                store.append_event(
                    {
                        "event": "restart_review_required",
                        "reason": (
                            "fresh dependency identity does not match a checkpointed "
                            "implementation review"
                        ),
                    }
                )

        artifacts = [
            "request.md",
            "acceptance-criteria.json",
            "roles.json",
            "classification.json",
            "context.json",
            contract,
        ]
        if contract.endswith("approved-plan.md"):
            artifacts.append("planning/plan-v1.md")
        if original.external_plan_artifact:
            artifacts.append(original.external_plan_artifact)
        if original.approval_required:
            artifacts.extend(["approval/human-approval.md", "approval/approved.json"])
        if from_stage == "review":
            validation_name, _ = _validation_evidence(source, code_digest)
            destination = copy_artifact(validation_name, "execution/validation-v1.json")
            store.update_manifest(validation_artifact=destination)
            artifacts.append(destination)
        elif from_stage == "verification":
            if requested_next == "verifier":
                validation_name, review_name, index_name = _verification_evidence(
                    source,
                    code_digest,
                    review_required=original.tier.value in {"standard", "substantial"},
                )
            else:
                validation_name, _ = _validation_evidence(source, code_digest)
                review_name = index_name = None
            validation_target = copy_artifact(validation_name, "execution/validation-v1.json")
            store.update_manifest(validation_artifact=validation_target)
            artifacts.append(validation_target)
            if review_name is not None and index_name is not None:
                review_target = copy_artifact(review_name, "review/implementation-review-v1.json")
                copy_artifact(index_name)
                artifacts.extend([review_target, index_name])
        reused = [
            name
            for name in artifacts
            if name.startswith(("execution/validation-", "review/implementation-review-"))
        ]
        if reused:
            store.append_event({"event": "restart_evidence_reused", "artifacts": reused})
        patch_target = store.write_bytes_artifact(
            "execution/restart-input.patch", patch_bytes, immutable=True
        )
        artifacts.append(patch_target.relative_to(store.root).as_posix())
        initial = "validation" if requested_next == "validation" else requested_next
        record_stage(store, registry, worktree, next_stage=initial, artifacts=artifacts)
        return resume_run(repo, config, registry, store.run_id, manual_resume=False)
    except Exception as exc:
        current = store.read_manifest()
        if current.status not in {RunState.FAILED, RunState.ESCALATED, RunState.CANCELLED}:
            if isinstance(exc, ScopeViolation):
                cause = "restart_governance_blocked"
            elif current.checkpoint is not None:
                cause = "restart_stage_failed"
            else:
                cause = "restart_setup_failed"
            store.update_manifest(
                status=RunState.ESCALATED,
                terminal_cause=cause,
                stop_code="scope_violation" if isinstance(exc, ScopeViolation) else None,
                terminal_reason=str(exc),
                failure_class="human_decision"
                if cause == "restart_governance_blocked"
                else "product",
                failure_reason=str(exc),
                failure_signature=f"{cause}:{'scope' if isinstance(exc, ScopeViolation) else 'restart'}",
            )
            store.append_event({"event": cause, "detail": str(exc)})
        cleanup_worktree_if_unproductive(Engine(store), repo, worktree)
        raise
