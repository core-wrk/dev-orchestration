"""The acceptance contract: what a change must satisfy, bound to the approved plan.

Saved once, beside the approved plan or trivial task contract, and reused unchanged on
every repair and resume. Runs saved before this existed have no contract and keep their
previous path.
"""

import hashlib
import json
import re
from pathlib import Path

from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, Tier
from dev_orchestration.domain.findings import Finding, ReviewResult, Verification
from dev_orchestration.domain.run import MAX_REMEDIATION_CYCLES
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.validation import ValidationOutcome

CONTRACT_ARTIFACT = "planning/acceptance-contract.json"
PRECHECK_NAME = "acceptance-precheck"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_contract(
    approved: str, project_config: ProjectConfig, tier: Tier, criteria: list[str]
) -> dict:
    return {
        "version": 1,
        "plan_sha256": _digest(approved),
        "validation": [
            {"name": name, "command": spec.command}
            for name, spec in project_config.validation.items()
            if tier in spec.required_for
        ],
        "checklist": list(criteria),
        "file_scope": {
            "include": list(project_config.scope.include),
            "exclude": list(project_config.scope.exclude),
        },
    }


def save_contract(
    store: RunStore,
    approved: str,
    project_config: ProjectConfig,
    tier: Tier,
    criteria: list[str],
) -> str:
    """Save the contract next to the plan it belongs to; a repeat save is a no-op."""
    if not (store.root / CONTRACT_ARTIFACT).is_file():
        store.write_json_artifact(
            CONTRACT_ARTIFACT,
            build_contract(approved, project_config, tier, criteria),
            immutable=True,
        )
    store.update_manifest(acceptance_contract_artifact=CONTRACT_ARTIFACT)
    return CONTRACT_ARTIFACT


def load_contract(store: RunStore, approved: str) -> dict | None:
    """Return the saved contract, or None for a run that never had one."""
    artifact = store.read_manifest().acceptance_contract_artifact
    if artifact is None:
        return None
    try:
        contract = json.loads((store.root / artifact).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContextContractError(f"saved acceptance contract is unreadable: {exc}") from exc
    if contract.get("plan_sha256") != _digest(approved):
        raise ContextContractError("saved acceptance contract does not match the approved plan")
    return contract


def contract_context(contract: dict) -> ContextRef:
    return ContextRef(
        label=Category.ACCEPTANCE_CONTRACT,
        path=CONTRACT_ARTIFACT,
        content=json.dumps(contract, indent=2, sort_keys=True),
    )


_NAMED_FILE = re.compile(r"(?<![\w/.:\-])[\w.\-]+(?:/[\w.\-]+)+\.\w+")
_REMOVAL = re.compile(r"\b(remov|delet|no longer|absent|not exist|gone)", re.IGNORECASE)


def _named_files(checklist: list[str]) -> list[str]:
    found: list[str] = []
    for item in checklist:
        if _REMOVAL.search(item):
            continue
        found.extend(_NAMED_FILE.findall(item))
    return sorted(set(found))


def precheck(contract: dict, changed_paths: list[str], root: Path) -> list[str]:
    """Cheap, deterministic checks run before any model reviews the change.

    It cannot judge whether a criterion is met; that stays with the reviewer and the
    verifier. It catches what is knowable without a model: an empty change, changes
    outside the contract's file scope, and an in-scope file that a checklist item names
    but that does not exist. Items about removing something are skipped.
    """
    failures: list[str] = []
    if not changed_paths:
        failures.append("the change is empty")
    fence = ScopeFence(
        include=contract["file_scope"]["include"], exclude=contract["file_scope"]["exclude"]
    )
    outside = fence.violations(changed_paths)
    if outside:
        failures.append(f"changed files outside the contract scope: {sorted(outside)}")
    missing = [
        path
        for path in _named_files(contract["checklist"])
        if fence.allows(path) and not (root / path).is_file()
    ]
    if missing:
        failures.append(f"checklist names files that do not exist: {missing}")
    return failures


class NoSafeRepairScope(RuntimeError):
    """A rejection names only files the run is not allowed to change."""


def repair_scope(
    gate: str,
    round_number: int,
    check: str,
    expected: str,
    named_files: list[str],
    fence: ScopeFence,
) -> dict:
    """Describe one repair: the gate, the failed check, the expected behavior, the files.

    A rejection that names files limits the repair to those files, provided the approved
    scope allows them. One that names none leaves the approved scope itself as the limit.
    One that names only files outside the approved scope has no safe repair.
    """
    named = sorted({path for path in named_files if path})
    allowed = [path for path in named if fence.allows(path)]
    if named and not allowed:
        raise NoSafeRepairScope(
            f"{gate} rejection names only files outside the approved scope: {named}"
        )
    return {
        "gate": gate,
        "round": round_number,
        "check": check,
        "expected": expected,
        "files": allowed or None,
        "restricted": bool(allowed),
    }


def save_repair_scope(store: RunStore, scope: dict) -> str:
    """Save the repair description so a resumed repair reads it instead of recomputing."""
    artifact = f"execution/repair-{scope['gate']}-v{scope['round']}.json"
    if not (store.root / artifact).is_file():
        store.write_json_artifact(artifact, scope, immutable=True)
    return artifact


def load_repair_scope(store: RunStore, gate: str, round_number: int) -> dict | None:
    path = store.root / f"execution/repair-{gate}-v{round_number}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def repair_context(scope: dict) -> ContextRef:
    return ContextRef(
        label=Category.REPAIR_SCOPE,
        path=None,
        content=json.dumps(scope, indent=2, sort_keys=True),
    )


def file_digests(root: Path, paths: list[str]) -> dict[str, str | None]:
    digests: dict[str, str | None] = {}
    for path in paths:
        target = root / path
        digests[path] = (
            hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        )
    return digests


def edited_paths(
    root: Path, before: dict[str, str | None], inventory_after: list[str]
) -> list[str]:
    """Files whose content changed since ``before`` was taken."""
    current = file_digests(root, sorted({*before, *inventory_after}))
    return sorted(path for path, digest in current.items() if before.get(path) != digest)


def repair_edit_violations(
    root: Path, before: dict[str, str | None], inventory_after: list[str], scope: dict
) -> list[str]:
    """Files a repair changed beyond the ones it was allowed to change."""
    if not scope["restricted"]:
        return []
    edited = edited_paths(root, before, inventory_after)
    return [path for path in edited if path not in scope["files"]]


def is_test_path(path: str) -> bool:
    """A test file by the usual naming conventions of Python and JavaScript projects."""
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in {"tests", "test", "__tests__"} for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
        or ".test." in name
        or ".spec." in name
    )


def validation_repair_scope(
    outcomes: list[ValidationOutcome], round_number: int, fence: ScopeFence
) -> dict:
    failed = [outcome for outcome in outcomes if not outcome.passed]
    return repair_scope(
        "validation",
        round_number,
        ", ".join(outcome.name for outcome in failed),
        "; ".join(f"{outcome.name} passes" for outcome in failed),
        [],
        fence,
    )


def review_repair_scope(review: ReviewResult, round_number: int, fence: ScopeFence) -> dict | None:
    """The next remediation round's description, or None when no round will follow."""
    if (
        not review.blocking_ids()
        or review.outcome in {Outcome.BLOCKED, Outcome.ESCALATE}
        or round_number > MAX_REMEDIATION_CYCLES
    ):
        return None
    blocking = [finding for finding in review.findings if finding.id in review.blocking_ids()]
    return repair_scope(
        "implementation_review",
        round_number,
        ", ".join(finding.id for finding in blocking),
        "; ".join(finding.evidence_required or finding.summary for finding in blocking),
        [finding.file for finding in blocking if finding.file],
        fence,
    )


def _unresolved_findings(verification: Verification, findings: list[Finding]) -> list[Finding]:
    by_id = {finding.id: finding for finding in findings}
    return [by_id[fid] for fid in verification.unresolved_finding_ids if fid in by_id]


def verification_route(
    verification: Verification, findings: list[Finding] | None = None
) -> str | None:
    """How a rejected final verification is repaired, decided by its criterion verdicts.

    ``product`` when any criterion failed; ``evidence`` when the only non-passing verdicts
    are UNKNOWN for want of a test; ``findings`` when every criterion passed and the rejection
    rests only on supplied review findings that each name a file; None when no worker repair
    can help. The verifier's own BLOCKED or CHANGES_REQUIRED label does not decide it; ESCALATE
    does.
    """
    if verification.outcome is Outcome.ESCALATE:
        return None
    rejected = [verdict for verdict in verification.verdicts if verdict.verdict != "PASS"]
    if any(verdict.verdict == "FAIL" for verdict in rejected):
        return "product"
    if rejected and all(verdict.missing_evidence == "test" for verdict in rejected):
        return "evidence"
    if rejected or not verification.verdicts:
        return None
    named = verification.unresolved_finding_ids
    unresolved = _unresolved_findings(verification, findings or [])
    if named and len(unresolved) == len(named) and all(f.file for f in unresolved):
        return "findings"
    return None


_EXPECTED = {
    "product": "every acceptance criterion has a PASS verdict",
    "evidence": "a test demonstrates each listed criterion, and every criterion has a PASS verdict",
}


def verification_repair_scope(
    verification: Verification,
    round_number: int,
    fence: ScopeFence,
    findings: list[Finding] | None = None,
) -> dict:
    mode = verification_route(verification, findings)
    if mode is None:
        raise NoSafeRepairScope(
            "final verification rejected the change without a criterion a worker can repair"
        )
    if mode == "findings":
        unresolved = _unresolved_findings(verification, findings or [])
        scope = repair_scope(
            "final_verification",
            round_number,
            ", ".join(finding.id for finding in unresolved),
            "; ".join(finding.evidence_required or finding.summary for finding in unresolved)
            + "; final verification accepts the change",
            [finding.file for finding in unresolved if finding.file],
            fence,
        )
    else:
        rejected = [verdict for verdict in verification.verdicts if verdict.verdict != "PASS"]
        scope = repair_scope(
            "final_verification",
            round_number,
            "; ".join(verdict.criterion for verdict in rejected),
            _EXPECTED[mode],
            [],
            fence,
        )
    scope["mode"] = mode
    return scope
