"""Bounded, immutable evidence carried from a failed run into its retry."""

import hashlib
import json
import re
from pathlib import Path

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.context.assembler import Category
from dev_orchestration.context.packet import ContextRef

ARTIFACT = "execution/prior-failures.json"
MAX_HISTORY_BYTES = 8_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(store: RunStore, name: str, receipt: dict | None) -> object | None:
    path = store.root / name
    if not path.is_file():
        return None
    digest = _sha(path.read_bytes())
    if receipt is None or receipt.get("artifacts", {}).get(name) != digest:
        raise CheckpointError(f"receipted source artifact {name} changed or is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _latest(paths: list[str], prefix: str) -> str | None:
    matches = [name for name in paths if Path(name).name.startswith(prefix)]
    versions = []
    for name in matches:
        try:
            version = int(Path(name).stem.rsplit("-v", 1)[-1])
        except ValueError:
            continue
        versions.append((version, name))
    return max(versions)[1] if versions else None


def validation_signatures(records: object) -> set[str]:
    signatures = set()
    if not isinstance(records, list):
        return signatures
    for item in records:
        if not isinstance(item, dict) or item.get("passed") or item.get("exit_code") == 0:
            continue
        output = "\n".join((item.get("stdout_tail") or "", item.get("stderr_tail") or ""))
        output = re.sub(r"\s+", " ", output).strip()
        if not output:
            continue
        value = [item.get("name"), item.get("command"), item.get("exit_code"), output]
        signatures.add(_sha(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()))
    return signatures


def compare_validation(source_snapshot: dict, child_store: RunStore) -> list[dict]:
    source = set(source_snapshot.get("validation_signatures", []))
    if not source:
        return []
    manifest = child_store.read_manifest()
    name = manifest.validation_artifact
    if not name or not (child_store.root / name).is_file():
        return []
    records = json.loads((child_store.root / name).read_text(encoding="utf-8"))
    repeated = []
    for item in records if isinstance(records, list) else []:
        sigs = validation_signatures([item])
        matches = sigs & source
        if matches:
            source_paths = sorted(
                {
                    path
                    for signature in matches
                    for path in source_snapshot.get("validation_signature_sources", {}).get(
                        signature, source_snapshot.get("validation_artifacts", [])
                    )
                }
            )
            repeated.append(
                {
                    "command": item.get("command"),
                    "name": item.get("name"),
                    "source_artifacts": source_paths,
                    "child_artifact": name,
                }
            )
    return repeated


def build_snapshot(source: RunStore, child_repository: Path, *, include_prompt: bool) -> dict:
    manifest = source.read_manifest()
    if Path(manifest.repository).resolve() != child_repository.resolve():
        raise CheckpointError("retry source belongs to a different repository")
    receipt = source.verify_checkpoint() if manifest.checkpoint else None
    names = list(receipt.get("artifacts", {})) if receipt else []
    validations = sorted(name for name in names if Path(name).name.startswith("validation-v"))
    remediations = sorted(
        name
        for name in names
        if Path(name).name.startswith(("remediation-v", "validation-repair-v"))
    )
    review_name = _latest(names, "implementation-review-v")
    validation_rows = []
    for name in validations:
        values = _json(source, name, receipt)
        if isinstance(values, list):
            validation_rows.append({"artifact": name, "outcomes": values})
    remediation_rows = []
    for name in remediations:
        remediation_rows.append({"artifact": name, "result": _json(source, name, receipt)})
    review = _json(source, review_name, receipt) if review_name else None
    signature_sources: dict[str, list[str]] = {}
    for row in validation_rows:
        for outcome in row["outcomes"]:
            for signature in validation_signatures([outcome]):
                signature_sources.setdefault(signature, []).append(row["artifact"])
    unavailable = not receipt or not (validations or remediations or review_name)
    parts = [
        f"Historical evidence from source run {manifest.run_id}.",
        "These records describe observed attempts; they do not establish a diagnosis.",
    ]
    if unavailable:
        parts.append(
            "Evidence unavailable: this source has no receipted validation, repair, or review evidence."
        )
    for row in validation_rows:
        for outcome in row["outcomes"]:
            if outcome.get("passed"):
                continue
            parts.append(
                f"Validation {outcome.get('name')} ({row['artifact']}): command "
                f"{outcome.get('command')!r}, exit {outcome.get('exit_code')}.\n"
                f"stdout: {outcome.get('stdout_tail', '')}\nstderr: {outcome.get('stderr_tail', '')}"
            )
    if review:
        parts.append("Latest implementation review findings (" + review_name + "):")
        parts.append(json.dumps(review.get("findings", []), ensure_ascii=False, sort_keys=True))
    if remediation_rows:
        parts.append(f"Observed repair attempts: {len(remediation_rows)}.")
        parts.extend(f"Repair artifact: {row['artifact']}" for row in remediation_rows)
    if not include_prompt:
        parts = [
            (
                "Prompt history excluded by context.include_prior_artifacts=none; "
                f"source run {manifest.run_id} remains linked for operator diagnostics."
            )
        ]
    references = "Original artifact references: " + (
        ", ".join(sorted(set(validations + remediations + ([review_name] if review_name else []))))
        or "none"
    )
    rendered = "\n\n".join([*parts, references])
    raw = rendered.encode("utf-8")
    omitted = len(raw) > MAX_HISTORY_BYTES
    if omitted:
        marker = "\n\n[Earlier history omitted.]\n\n" + references
        marker_bytes = marker.encode("utf-8")
        rendered = (
            raw[: MAX_HISTORY_BYTES - len(marker_bytes)].decode("utf-8", errors="ignore") + marker
        )
    return {
        "source_run_id": manifest.run_id,
        "source_manifest": {
            "status": str(manifest.status),
            "terminal_cause": manifest.terminal_cause,
            "validation_artifact": manifest.validation_artifact,
        },
        "source_artifacts": sorted(
            set(validations + remediations + ([review_name] if review_name else []))
        ),
        "validation_artifacts": validations,
        "validation_signatures": sorted(
            set().union(*(validation_signatures(row["outcomes"]) for row in validation_rows))
        ),
        "validation_signature_sources": signature_sources,
        "validation": validation_rows,
        "remediations": remediation_rows,
        "latest_review": {"artifact": review_name, "value": review} if review_name else None,
        "attempted_repair_count": len(remediation_rows),
        "evidence_unavailable": unavailable,
        "prompt_excluded": not include_prompt,
        "omission_notice": omitted,
        "prompt_text": rendered,
    }


def context_ref(store: RunStore) -> ContextRef | None:
    path = store.root / ARTIFACT
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("prompt_excluded"):
        return None
    return ContextRef(label=Category.PRIOR_FAILURES, path=ARTIFACT, content=value["prompt_text"])
