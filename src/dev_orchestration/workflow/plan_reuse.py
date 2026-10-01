"""Evidence for safely reusing a completed plan review."""

import hashlib
import json
import re
from pathlib import Path

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.domain.enums import Outcome
from dev_orchestration.domain.findings import ReviewResult
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.roles.loader import load_role_prompt

RECEIPT = "planning/plan-review-receipt.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _review_inputs(
    manifest: RunManifest,
    repo_root: Path,
    plan_bytes: bytes,
    profile_constraints: list | None = None,
) -> dict:
    roles = {}
    for role in ("plan_reviewer", "plan_reconciler"):
        binding = manifest.roles.get(role)
        roles[role] = {
            "binding": binding.model_dump(mode="json") if binding else None,
            "prompt_sha256": _sha(load_role_prompt(role).encode()),
        }
    context = []
    for name in manifest.context_included:
        path = repo_root / name
        if not path.is_file():
            raise CheckpointError(f"included review context {name} is missing")
        context.append({"path": name, "sha256": _sha(path.read_bytes())})
    scope = {}
    if manifest.config_layers:
        scope = manifest.config_layers[0].get("scope", {})
    return {
        "plan_sha256": _sha(plan_bytes),
        "base_commit": manifest.git.base_commit,
        "request": manifest.request_text,
        "criteria": manifest.acceptance_criteria,
        "effective_tier": str(manifest.tier),
        "profiles": {
            "available": manifest.available_profiles,
            "active": manifest.active_profiles,
        },
        "profile_constraints": [
            {"path": item.path, "content": item.content} for item in (profile_constraints or [])
        ],
        "scope": scope,
        "resolved_policy": manifest.resolved_config,
        "context": context,
        "review_roles": roles,
    }


def create_receipt(
    store: RunStore,
    plan_path: Path,
    review_path: Path,
    profile_constraints: list | None = None,
) -> dict:
    """Create immutable evidence for the accepted final review."""
    manifest = store.read_manifest()
    inputs = _review_inputs(manifest, store.repo_root, plan_path.read_bytes(), profile_constraints)
    review_name = review_path.relative_to(store.root).as_posix()
    review_digest = _sha(review_path.read_bytes())
    receipt = {
        **inputs,
        "plan_version": plan_path.name,
        "final_review": {"path": review_name, "sha256": review_digest},
    }
    store.write_json_artifact(RECEIPT, receipt, immutable=True)
    return receipt


def validate_source(source: RunStore) -> tuple[dict | None, bytes | None]:
    """Verify source checkpoint and review evidence before offering it for reuse."""
    try:
        manifest = source.read_manifest()
        approved_path = source.root / "planning" / "approved-plan.md"
        if manifest.plan_sha256 and (
            not approved_path.is_file() or _sha(approved_path.read_bytes()) != manifest.plan_sha256
        ):
            raise CheckpointError("source approved plan hash is missing or changed")
        if manifest.checkpoint is None:
            if (source.root / RECEIPT).exists():
                raise CheckpointError("plan review receipt has no source checkpoint")
            return None, None
        checkpoint = source.verify_checkpoint()
        receipt_path = source.root / RECEIPT
        if not receipt_path.is_file():
            return None, None
        if RECEIPT not in checkpoint["artifacts"]:
            raise CheckpointError("plan review receipt is not checkpointed")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        version = receipt.get("plan_version")
        if (
            not isinstance(version, str)
            or not re.fullmatch(r"plan-v\d+\.md", version)
            or version != manifest.approved_plan_version
        ):
            raise CheckpointError("plan review receipt does not match the approved plan version")
        plan_name = f"planning/{version}"
        if (
            plan_name not in checkpoint["artifacts"]
            or "planning/approved-plan.md" not in checkpoint["artifacts"]
        ):
            raise CheckpointError("approved plan evidence is not checkpointed")
        plan_path = source.root / "planning" / version
        if not plan_path.is_file() or not approved_path.is_file():
            raise CheckpointError("reviewed approved plan is missing")
        plan_bytes = plan_path.read_bytes()
        if plan_bytes != approved_path.read_bytes() or _sha(plan_bytes) != receipt.get(
            "plan_sha256"
        ):
            raise CheckpointError("reviewed plan evidence changed")
        review = receipt.get("final_review")
        if not isinstance(review, dict):
            raise CheckpointError("final plan review evidence is missing")
        review_name = review.get("path")
        if (
            not isinstance(review_name, str)
            or Path(review_name).is_absolute()
            or ".." in Path(review_name).parts
        ):
            raise CheckpointError("final plan review path is invalid")
        if review_name not in checkpoint["artifacts"]:
            raise CheckpointError("final plan review is not checkpointed")
        review_path = source.root / review_name
        if not review_path.is_file() or _sha(review_path.read_bytes()) != review.get("sha256"):
            raise CheckpointError("final plan review evidence changed")
        result = ReviewResult.model_validate_json(review_path.read_text(encoding="utf-8"))
        if result.outcome in {Outcome.BLOCKED, Outcome.ESCALATE} or result.blocking_ids():
            raise CheckpointError("source plan review was not accepted")
        index_name = "review/finding-index.json"
        if index_name not in checkpoint["artifacts"]:
            raise CheckpointError("source plan review finding index is not checkpointed")
        finding_index = source.finding_index()
        if not isinstance(finding_index, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in finding_index.items()
        ):
            raise CheckpointError("source plan review finding index is invalid")
        if not {finding.id for finding in result.findings}.issubset(set(finding_index.values())):
            raise CheckpointError("source plan review finding IDs are missing from its index")
        return receipt, plan_bytes
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckpointError("source plan review evidence is invalid") from exc


def match_source(
    source: RunStore,
    *,
    current: RunStore,
    plan_bytes: bytes,
    profile_constraints: list | None = None,
) -> tuple[bool, str, dict | None]:
    receipt, reviewed_bytes = validate_source(source)
    if receipt is None or reviewed_bytes is None:
        return False, "source has no plan review receipt; review will run", None
    expected = _review_inputs(
        current.read_manifest(), current.repo_root, plan_bytes, profile_constraints
    )
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches:
        return False, f"plan review inputs changed ({', '.join(mismatches)}); review will run", None
    return True, "matching completed plan review receipt", receipt
