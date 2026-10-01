"""Bootstrap a run and join configuration with safe runtime context."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from dev_orchestration.artifacts.store import RunStore, new_run_id, slugify
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.config.resolver import (
    PROTECTED_DEFAULTS,
    ProtectedRuleViolation,
    resolve_policy,
)
from dev_orchestration.context.assembler import ContextContractError, read_reference
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import RunState, Tier
from dev_orchestration.domain.run import GitBlock, RunManifest
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.git.worktree import create_worktree


class BootstrapIntegrityError(RuntimeError):
    """The base or isolated worktree changed during bootstrap."""


@dataclass(frozen=True)
class RuntimeContext:
    invariants: list[ContextRef]
    references: list[ContextRef]
    profile_constraints: list[ContextRef]
    included: list[str]
    excluded: list[str]
    conflicts: list[str]
    minimum_tier: Tier | None


CLASSIFICATION_REFERENCE_BUDGET = 16_000
_MARKDOWN_PATH = re.compile(r"/?[A-Za-z0-9_./-]+\.md")


def _request_markdown_paths(request_text: str) -> list[str]:
    """Extract explicit Markdown paths without discovering repository files."""
    found: list[str] = []
    for match in _MARKDOWN_PATH.finditer(request_text):
        path = match.group(0)
        # A path inside a URL is not a repository-relative source.
        prefix = request_text[max(0, request_text.rfind(" ", 0, match.start()) + 1) : match.start()]
        if "://" in prefix:
            continue
        if path not in found:
            found.append(path)
    return found


def prepare_classification_references(
    repo_root: Path,
    project_config: ProjectConfig,
    request_text: str,
    fence,
) -> tuple[list[ContextRef], dict]:
    """Load only explicit Markdown paths authorized by declared context policy."""
    candidates = _request_markdown_paths(request_text)
    persistent = set(project_config.context.persistent)
    on_demand = tuple(project_config.context.on_demand.values())
    refs: list[ContextRef] = []
    sources: list[dict] = []
    used = 0

    for path in candidates:
        pure = PurePosixPath(path)
        authorized = (
            not pure.is_absolute()
            and ".." not in pure.parts
            and (
                path in persistent
                or any(
                    path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/")
                    for root in on_demand
                )
            )
        )
        if not authorized:
            reason = "not authorized by context.persistent or context.on_demand"
            refs.append(ContextRef(label="references", path=path, content=f"[rejected: {reason}]"))
            sources.append({"path": path, "status": "rejected", "reason": reason})
            continue
        try:
            reference = read_reference(repo_root, path, fence)
            raw = (repo_root / path).resolve().read_bytes()
            decoded = raw.decode("utf-8")
        except (ContextContractError, OSError, UnicodeError) as exc:
            reason = str(exc)
            refs.append(ContextRef(label="references", path=path, content=f"[rejected: {reason}]"))
            missing = not (repo_root / path).exists()
            sources.append(
                {"path": path, "status": "missing" if missing else "rejected", "reason": reason}
            )
            continue

        digest = hashlib.sha256(raw).hexdigest()
        size = len(raw)
        remaining = max(0, CLASSIFICATION_REFERENCE_BUDGET - used)
        excerpt = decoded.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        used += len(excerpt.encode("utf-8"))
        truncated = len(excerpt.encode("utf-8")) < len(decoded.encode("utf-8"))
        notice = "\n[truncated; original content continues]" if truncated else ""
        content = f"Original bytes: {size}\nSHA-256: {digest}\n{excerpt}{notice}"
        refs.append(reference.model_copy(update={"content": content}))
        sources.append(
            {"path": path, "status": "included", "sha256": digest, "original_bytes": size}
        )

    artifact = {
        "sources": sources,
        "supplied_references": [item.model_dump(mode="json") for item in refs],
    }
    return refs, artifact


def classification_references_for_run(
    store: RunStore,
    project_config: ProjectConfig,
    request_text: str,
    fence,
    *,
    persist_if_missing: bool,
) -> list[ContextRef]:
    """Reuse the immutable classifier packet when present, including on resume."""
    artifact_path = store.root / "classification-inputs.json"
    if artifact_path.is_file():
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            return [ContextRef.model_validate(item) for item in artifact["supplied_references"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ContextContractError("saved classification inputs are invalid") from exc
    refs, artifact = prepare_classification_references(
        store.repo_root, project_config, request_text, fence
    )
    if persist_if_missing:
        store.write_json_artifact("classification-inputs.json", artifact, immutable=True)
    return refs


def _tier_rank(tier: Tier) -> int:
    return {
        Tier.TRIVIAL: 0,
        Tier.STANDARD: 1,
        Tier.SUBSTANTIAL: 2,
        Tier.HIGH_RISK: 3,
    }[tier]


def resolve_run_policy(layers: list[dict]) -> dict:
    """Resolve layers through the non-overridable policy resolver."""
    return resolve_policy(layers)


def resolve_runtime_context(
    repo_root: Path,
    project_config: ProjectConfig,
    active_profiles: list[str],
    fence,
) -> RuntimeContext:
    """Load only declared, safely readable context for active profiles."""
    available = set(project_config.profiles.available)
    unknown = sorted(set(active_profiles) - available)
    if unknown:
        raise ContextContractError(
            f"classifier activated profiles not available to this repository: {unknown}"
        )

    invariants: list[ContextRef] = []
    references: list[ContextRef] = []
    profile_constraints: list[ContextRef] = []
    included: list[str] = []
    excluded: list[str] = []
    conflicts: list[str] = []

    def load(path: str, label: str) -> None:
        candidate = repo_root / path
        if not candidate.exists():
            excluded.append(f"{path}: missing")
            return
        try:
            reference = read_reference(repo_root, path, fence)
        except ContextContractError as exc:
            excluded.append(f"{path}: {exc}")
            return
        if label == "invariants":
            budget = project_config.context.persistent_budget_bytes
            used = sum(len(item.content.encode("utf-8")) for item in invariants)
            size = len(reference.content.encode("utf-8"))
            if used + size > budget:
                excluded.append(f"{path}: over the {budget}-byte persistent context budget")
                return
            invariants.append(reference.model_copy(update={"label": label}))
        else:
            references.append(reference)
        included.append(path)

    for path in project_config.context.persistent:
        load(path, "invariants")

    minimum_tier: Tier | None = None
    seen_controls: dict[str, object] = {}
    for profile_name in active_profiles:
        profile = project_config.profiles.definitions.get(profile_name)
        if profile is None:
            profile = project_config.profiles.definitions.get(profile_name.replace("-", "_"))
        if profile is None:
            excluded.append(f"profile:{profile_name}: no local definition")
            profile_constraints.append(
                ContextRef(
                    label="profile_constraints",
                    path=f"profile:{profile_name}",
                    content=json.dumps({"profile": profile_name, "definition": None}),
                )
            )
            continue
        if profile.minimum_tier and (
            minimum_tier is None or _tier_rank(profile.minimum_tier) > _tier_rank(minimum_tier)
        ):
            minimum_tier = profile.minimum_tier
        for key, value in profile.controls.items():
            if key in seen_controls and seen_controls[key] != value:
                conflicts.append(
                    f"profile control {key!r} conflicts: {seen_controls[key]!r} vs {value!r}"
                )
            seen_controls[key] = value
        profile_constraints.append(
            ContextRef(
                label="profile_constraints",
                path=f"profile:{profile_name}",
                content=json.dumps(
                    {
                        "profile": profile_name,
                        "minimum_tier": profile.minimum_tier,
                        "controls": profile.controls,
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
        )
        for path in profile.context_refs:
            load(path, "references")

    if conflicts and project_config.context.conflict_policy == "fail_closed":
        raise ContextContractError(
            "conflicting active profile constraints: " + "; ".join(conflicts)
        )
    return RuntimeContext(
        invariants=invariants,
        references=references,
        profile_constraints=profile_constraints,
        included=included,
        excluded=excluded,
        conflicts=conflicts,
        minimum_tier=minimum_tier,
    )


def bootstrap_run(
    repo: GitRepo,
    project_config: ProjectConfig,
    request_text: str,
    tier: Tier,
    worktree_root: Path,
) -> tuple[RunStore, Path]:
    """Create the durable run record and isolated worktree from a clean base."""
    repo.ensure_clean()
    policy = resolve_run_policy([project_config.model_dump(mode="json")])
    for key in PROTECTED_DEFAULTS:
        if policy.get(key) is not False:
            raise ProtectedRuleViolation(
                f"{key} resolved to {policy.get(key)!r}; protected rules cannot be overridden"
            )

    slug = slugify(request_text)[:40] or "run"
    run_id = new_run_id(str(tier), slug)
    run_root = repo.root / ".ai" / "runs" / run_id
    suffix = 2
    while run_root.exists():
        run_id = f"{new_run_id(str(tier), slug)}-{suffix}"
        run_root = repo.root / ".ai" / "runs" / run_id
        suffix += 1
    branch = f"{project_config.git.branch_prefix}{run_id}-{slug}"
    worktree = worktree_root / project_config.project.name / run_id
    base_commit = repo.current_commit()
    store = RunStore(repo.root, run_id)
    store.initialize(
        RunManifest(
            run_id=run_id,
            repository=str(repo.root),
            workflow=str(tier),
            tier=tier,
            project_class=project_config.project.project_class,
            request_text=request_text,
            created_at=datetime.now(UTC),
            resolved_config=policy,
            git=GitBlock(base_commit=base_commit, branch=branch),
        )
    )
    try:
        create_worktree(repo, worktree, branch)
    except Exception as exc:
        store.update_manifest(
            status=RunState.FAILED,
            completed_at=datetime.now(UTC),
            terminal_reason=str(exc),
        )
        store.append_event({"event": "bootstrap_failed", "detail": str(exc)})
        raise
    store.update_manifest(
        git=GitBlock(base_commit=base_commit, branch=branch, worktree=str(worktree))
    )
    current_commit = repo.current_commit()
    worktree_repo = GitRepo(worktree)
    if current_commit != base_commit:
        reason = (
            f"base commit moved during bootstrap: recorded {base_commit}, current {current_commit}"
        )
        store.update_manifest(
            status=RunState.FAILED,
            completed_at=datetime.now(UTC),
            terminal_reason=reason,
        )
        store.append_event({"event": "bootstrap_failed", "detail": reason})
        raise BootstrapIntegrityError(reason)
    if not worktree_repo.is_ancestor(base_commit):
        reason = f"run worktree {worktree} does not descend from recorded base {base_commit}"
        store.update_manifest(
            status=RunState.FAILED,
            completed_at=datetime.now(UTC),
            terminal_reason=reason,
        )
        store.append_event({"event": "bootstrap_failed", "detail": reason})
        raise BootstrapIntegrityError(reason)
    store.append_event({"event": "run_bootstrapped", "run_id": run_id, "branch": branch})
    return store, worktree
