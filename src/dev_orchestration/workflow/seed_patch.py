"""Immutable seed patch input and its one-time worktree application."""

import hashlib
import json

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.scope_check import (
    ScopeViolation,
    enforce_fence,
    enforce_ignored_writes,
)

PATCH_ARTIFACT = "execution/seed-patch.patch"
RECEIPT_ARTIFACT = "execution/seed-patch-receipt.json"


def save_seed_patch(store: RunStore, patch: bytes) -> str:
    """Persist caller-supplied bytes before they can be applied to a worktree."""
    digest = hashlib.sha256(patch).hexdigest()
    store.write_bytes_artifact(PATCH_ARTIFACT, patch, immutable=True)
    store.write_json_artifact(
        RECEIPT_ARTIFACT,
        {"artifact": PATCH_ARTIFACT, "sha256": digest},
        immutable=True,
    )
    return digest


def apply_seed_patch_once(
    store: RunStore, worktree: GitRepo, base_commit: str, fence: ScopeFence
) -> bool:
    """Apply the checkpointed patch once, rolling back if it crosses the fence."""
    manifest = store.read_manifest()
    if not manifest.seed_patch_artifact:
        return False
    events_path = store.root / "events.jsonl"
    if events_path.is_file() and any(
        json.loads(line).get("event") == "seed_patch_applied"
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ):
        return False

    artifact = store.root / manifest.seed_patch_artifact
    receipt_path = store.root / RECEIPT_ARTIFACT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        patch = artifact.read_bytes()
    except (OSError, ValueError) as exc:
        raise CheckpointError("saved seed patch receipt is missing or invalid") from exc
    digest = hashlib.sha256(patch).hexdigest()
    if (
        receipt != {"artifact": manifest.seed_patch_artifact, "sha256": digest}
        or digest != manifest.seed_patch_sha256
    ):
        raise CheckpointError("saved seed patch bytes do not match their receipt")

    ignored_before = worktree.ignored_paths()
    worktree.apply_patch_bytes(patch)
    try:
        enforce_fence(worktree, base_commit, fence)
        enforce_ignored_writes(worktree, ignored_before, fence)
    except ScopeViolation:
        worktree.apply_patch_bytes(patch, reverse=True)
        worktree.restore_ignored_snapshot(ignored_before)
        raise
    store.append_event({"event": "seed_patch_applied", "sha256": digest})
    return True
