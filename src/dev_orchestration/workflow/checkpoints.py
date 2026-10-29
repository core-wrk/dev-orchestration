"""Input and output evidence used by durable stage receipts."""

import hashlib
import json
from pathlib import Path

from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.git.repo import GitCommandError, GitRepo
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.workflow.snapshot import capture_snapshot


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _within(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise CheckpointError(f"input path escaped repository: {name}")
    return target


def project_config_matches(saved: dict, current: ProjectConfig) -> bool:
    """Compare saved config while accepting legacy omissions of false/empty defaults."""
    normalized = dict(saved)
    normalized.setdefault("validation_files", [])
    context = dict(normalized.get("context", {}))
    context.setdefault("dependency_discovery", False)
    normalized["context"] = context
    return normalized == current.model_dump(mode="json")


def dependency_discovery_default_omission(saved: dict, current: ProjectConfig) -> bool:
    """Recognize old saved policy that predates the default-disabled discovery option."""
    return (
        "dependency_discovery" not in saved.get("context", {})
        and not current.context.dependency_discovery
    )


def validation_files_default_omission(repo: GitRepo, base_commit: str) -> bool:
    """Recognize only adding or omitting the explicit empty validation_files default."""
    if not base_commit:
        return False
    try:
        diff = repo.run_git(
            "diff", "--no-ext-diff", "--unified=0", base_commit, "--", ".ai/project.yaml"
        )
    except GitCommandError:
        return False
    changed = [
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return len(changed) == 1 and all(line[1:].strip() == "validation_files: []" for line in changed)


def input_hashes(
    store: RunStore,
    registry: RoleRegistry,
    *,
    manifest_overrides: dict[str, object] | None = None,
    allow_changed_dependency_sources: bool = False,
) -> dict[str, str]:
    manifest = store.read_manifest()
    fields = manifest.model_dump(
        mode="json",
        include={
            "run_id",
            "repository",
            "workflow",
            "tier",
            "project_class",
            "available_profiles",
            "active_profiles",
            "plan_origin",
            "plan_sha256",
            "approved_plan_version",
            "request_text",
            "acceptance_criteria",
            "acceptance_criteria_source",
            "tier_override",
            "downgrade_reason",
            "approval_required",
            "minimum_tier",
            "context_included",
            "context_excluded",
            "context_conflicts",
            "config_layers",
            "roles",
            "resolved_config",
            "external_plan_artifact",
            "seed_patch_artifact",
            "seed_patch_sha256",
        },
    )
    if manifest_overrides:
        fields.update(manifest_overrides)
    if manifest.restart_mode is not None:
        fields["restart"] = {
            "linked_source_run": manifest.linked_source_run,
            "restart_mode": manifest.restart_mode,
            "restart_from": manifest.restart_from,
            "restart_reason": manifest.restart_reason,
            "restart_code_sha256": manifest.restart_code_sha256,
            "restart_source_base_commit": manifest.restart_source_base_commit,
            "restart_destination_base_commit": manifest.restart_destination_base_commit,
        }
    if manifest.plan_review_provenance:
        fields["plan_review_provenance"] = manifest.plan_review_provenance
    values = {"manifest_contract": _digest(json.dumps(fields, sort_keys=True).encode())}
    if manifest.validation_input_hashes:
        values["validation_input_hashes"] = _digest(
            json.dumps(manifest.validation_input_hashes, sort_keys=True).encode()
        )
    for name in (".ai/project.yaml", *manifest.context_included):
        path = _within(store.repo_root, name)
        if name == ".ai/project.yaml" and not path.exists():
            continue  # Direct library callers may supply an in-memory config.
        if not path.is_file():
            raise CheckpointError(f"run input {name} is missing")
        values[name] = _digest(path.read_bytes())
    for name in ("request.md", "acceptance-criteria.json", "roles.json"):
        path = store.root / name
        if not path.is_file():
            raise CheckpointError(f"run input {name} is missing")
        values[name] = _digest(path.read_bytes())
    prior_failures = store.root / "execution/prior-failures.json"
    if prior_failures.is_file():
        values["execution/prior-failures.json"] = _digest(prior_failures.read_bytes())
    classification_inputs = store.root / "classification-inputs.json"
    if classification_inputs.is_file():
        values["classification-inputs.json"] = _digest(classification_inputs.read_bytes())
        try:
            saved = json.loads(classification_inputs.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckpointError("saved classification inputs are invalid") from exc
        for source in saved.get("sources", []):
            status = source.get("status")
            if status not in {"included", "missing"}:
                continue
            name = source.get("path")
            if not isinstance(name, str):
                raise CheckpointError("saved classification source path is invalid")
            path = _within(store.repo_root, name)
            if status == "missing":
                if path.exists():
                    raise CheckpointError(f"classification source {name} appeared after checkpoint")
                values[f"classification_source:{name}"] = "missing"
                continue
            if not path.is_file():
                raise CheckpointError(f"classification source {name} is missing")
            digest = _digest(path.read_bytes())
            if source.get("sha256") != digest:
                raise CheckpointError(f"classification source {name} changed after selection")
            values[f"classification_source:{name}"] = digest
    dependency_path = store.root / "dependency-report.json"
    if dependency_path.is_file():
        values["dependency-report.json"] = _digest(dependency_path.read_bytes())
        try:
            dependency = json.loads(dependency_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckpointError("saved dependency report is invalid") from exc
        if dependency.get("declared_sources") is not None:
            from dev_orchestration.workflow.dependencies import dependency_sources

            config = ProjectConfig.model_validate(manifest.config_layers[0])
            _, inventory = dependency_sources(store.repo_root, config)
            current_identity = _digest(
                json.dumps(
                    {
                        "declared_sources": inventory["declared_sources"],
                        "sources": inventory["sources"],
                        "inaccessible_sources": inventory["inaccessible_sources"],
                    },
                    sort_keys=True,
                ).encode()
            )
            if current_identity != dependency.get("source_inventory_identity"):
                if not allow_changed_dependency_sources:
                    raise CheckpointError(
                        "authorized dependency source inventory changed after discovery"
                    )
                values["dependency_inventory"] = current_identity
            else:
                values["dependency_inventory"] = current_identity
        for source in dependency.get("source_identities", []):
            name, expected = source.get("path"), source.get("sha256")
            if not isinstance(name, str) or not isinstance(expected, str):
                raise CheckpointError("dependency source identity is invalid")
            path = _within(store.repo_root, name)
            if not path.is_file() or _digest(path.read_bytes()) != expected:
                if allow_changed_dependency_sources:
                    continue
                raise CheckpointError(f"dependency source {name} changed after discovery")
            values[f"dependency_source:{name}"] = expected
    if manifest.external_plan_artifact:
        path = store.root / manifest.external_plan_artifact
        if not path.is_file():
            raise CheckpointError("saved external plan is missing")
        values[manifest.external_plan_artifact] = _digest(path.read_bytes())
    if manifest.seed_patch_artifact:
        path = store.root / manifest.seed_patch_artifact
        if not path.is_file():
            raise CheckpointError("saved seed patch is missing")
        values[manifest.seed_patch_artifact] = _digest(path.read_bytes())
    for role in manifest.roles:
        values[f"role:{role}"] = _digest(load_role_prompt(role).encode())
        binding = registry.binding_for(role)
        saved = manifest.roles[role]
        if (binding.adapter, binding.model, binding.reasoning, binding.timeout_seconds) != (
            saved.adapter,
            saved.model_alias,
            saved.reasoning,
            saved.timeout_seconds,
        ):
            raise CheckpointError(f"role binding changed: {role}")
        adapter = registry.adapters.get(saved.adapter)
        binary = getattr(adapter, "binary", None)
        if saved.resolved_binary != (str(binary) if binary is not None else None):
            raise CheckpointError(f"provider binary changed for role {role}")
    return values


def record_stage(
    store: RunStore,
    registry: RoleRegistry,
    worktree: Path,
    *,
    next_stage: str,
    artifacts: list[str],
    cycle: int = 0,
) -> dict:
    base = store.read_manifest().git.base_commit
    if not base:
        raise CheckpointError("run has no recorded base commit")
    prior = store.root / "execution/prior-failures.json"
    if prior.is_file() and "execution/prior-failures.json" not in artifacts:
        artifacts = [*artifacts, "execution/prior-failures.json"]
    dependency_report = store.root / "dependency-report.json"
    if dependency_report.is_file() and "dependency-report.json" not in artifacts:
        artifacts = [*artifacts, "dependency-report.json"]
    return store.checkpoint(
        next_stage=next_stage,
        artifacts=artifacts,
        worktree=capture_snapshot(GitRepo(worktree), base),
        inputs=input_hashes(store, registry),
        cycle=cycle,
        attempt=store.read_manifest().stage_attempts.get(next_stage, 0) + 1,
    )
