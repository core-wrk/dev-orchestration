"""Provider-neutral workflow stages."""

import difflib
import json
import re
from pathlib import Path

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.context.assembler import Category, assemble
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import ProjectClass
from dev_orchestration.domain.findings import (
    Classification,
    Finding,
    ReviewResult,
    Verification,
    next_finding_id,
)
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.workflow.invoke import AgentInvocationError, invoke_structured


def _request(registry: RoleRegistry, role: str, store: RunStore, packet, cwd: Path):
    return registry.build_request(role, prompt=load_role_prompt(role), cwd=cwd, context=packet)


def _raw_text(output: dict | str) -> str:
    return output if isinstance(output, str) else json.dumps(output, indent=2) + "\n"


def _agent_result_json(result: AgentResult) -> dict:
    return {
        "provider": result.provider,
        "model": result.model,
        "exit_code": result.exit_code,
        "output": result.output,
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
        "usage": result.usage,
    }


def _record_result(store: RunStore, name: str, value: object) -> None:
    store.write_json_artifact(name, value)


def classify(
    registry: RoleRegistry,
    request_text: str,
    project_class: ProjectClass,
    store: RunStore,
    available_profiles: list[str] | None = None,
) -> Classification:
    profiles = available_profiles or []
    repo_context = f"{project_class}\nAvailable profiles: {', '.join(profiles) or '(none)'}"
    packet = assemble(
        "classification",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            ContextRef(label=Category.REPO_CLASS, path=None, content=repo_context),
        ],
    )
    request = _request(registry, "classifier", store, packet, store.repo_root)
    result = invoke_structured(
        registry.adapter_for("classifier"), request, Classification, store.root / "schemas"
    )
    _record_result(store, "classification.json", result.model_dump(mode="json"))
    store.append_event(
        {
            "event": "classified",
            "tier": str(result.tier),
            "rationale": result.rationale,
            "profiles": result.profiles,
        }
    )
    return result


def plan(
    registry: RoleRegistry,
    brief: str,
    invariants: list[ContextRef],
    references: list[ContextRef],
    store: RunStore,
) -> Path:
    packet = assemble(
        "planning",
        [ContextRef(label=Category.BRIEF, path=None, content=brief), *invariants, *references],
    )
    request = _request(registry, "planner", store, packet, store.repo_root)
    result = registry.adapter_for("planner").run(request)
    store.write_json_artifact("execution/planner-result.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(
            f"planner provider {result.provider!r} exited with {result.exit_code}"
        )
    text = _raw_text(result.output)
    path = store.write_plan_version(text)
    store.append_event({"event": "plan_written", "version": path.name})
    return path


def review_plan(
    registry: RoleRegistry,
    brief: str,
    plan_text: str,
    profile_constraints: list[ContextRef],
    store: RunStore,
) -> ReviewResult:
    packet = assemble(
        "plan_review",
        [
            ContextRef(label=Category.BRIEF, path=None, content=brief),
            ContextRef(label=Category.PROPOSED_PLAN, path=None, content=plan_text),
            *profile_constraints,
        ],
    )
    request = _request(registry, "plan_reviewer", store, packet, store.repo_root)
    raw = invoke_structured(
        registry.adapter_for("plan_reviewer"), request, ReviewResult, store.root / "schemas"
    )
    result = _renumber(raw, store)
    store.write_versioned_json("plan-review", result.model_dump(mode="json"))
    store.append_event(
        {
            "event": "plan_reviewed",
            "outcome": str(result.outcome),
            "finding_ids": [finding.id for finding in result.findings],
        }
    )
    return result


class FindingIdentityAmbiguity(RuntimeError):
    """A review round could not be matched to prior finding identity safely."""


def _canonical_summary(summary: str) -> str:
    words = re.findall(r"[a-z0-9]+", summary.casefold())
    return " ".join(word[:-1] if word.endswith("s") and len(word) > 4 else word for word in words)


def _fingerprint(finding: Finding) -> str:
    """Identify a defect without volatile line numbers or exact phrasing."""
    return f"{finding.file or '<none>'}|{_canonical_summary(finding.summary)}"


def _matching_key(key: str, finding: Finding) -> bool:
    file_name, _, summary = key.partition("|")
    if file_name != (finding.file or "<none>"):
        return False
    current = _canonical_summary(finding.summary)
    ratio = difflib.SequenceMatcher(None, summary, current).ratio()
    old_words = set(summary.split())
    new_words = set(current.split())
    overlap = len(old_words & new_words) / max(len(old_words | new_words), 1)
    return ratio >= 0.75 or overlap >= 0.8


def _renumber(result: ReviewResult, store: RunStore) -> ReviewResult:
    index = store.finding_index()
    existing = list(index.values())
    used: set[str] = set()
    renumbered: list[Finding] = []
    for finding in result.findings:
        key = _fingerprint(finding)
        finding_id = index.get(key)
        if finding_id is None:
            matches = [
                candidate_id
                for candidate_key, candidate_id in index.items()
                if _matching_key(candidate_key, finding)
            ]
            matches = sorted(set(matches))
            if len(matches) > 1:
                raise FindingIdentityAmbiguity(
                    f"finding {finding.summary!r} ambiguously matches prior IDs {matches}"
                )
            finding_id = matches[0] if matches else next_finding_id(existing)
            index[key] = finding_id
            existing.append(finding_id)
        if finding_id in used:
            raise FindingIdentityAmbiguity(
                f"review round contains more than one distinct finding mapped to {finding_id}"
            )
        used.add(finding_id)
        renumbered.append(finding.model_copy(update={"id": finding_id}))
    store.record_finding_index(index)
    return result.model_copy(update={"findings": renumbered})


def reconcile(
    registry: RoleRegistry,
    plan_text: str,
    findings: list[Finding],
    store: RunStore,
) -> Path:
    packet = assemble(
        "reconciliation",
        [
            ContextRef(label=Category.PROPOSED_PLAN, path=None, content=plan_text),
            ContextRef(
                label=Category.REVIEW_FINDINGS,
                path=None,
                content="\n".join(
                    f"{finding.id} [{finding.severity}] {finding.summary}\n"
                    f"  required evidence: {finding.evidence_required}"
                    for finding in findings
                ),
            ),
        ],
    )
    request = _request(registry, "plan_reconciler", store, packet, store.repo_root)
    result = registry.adapter_for("plan_reconciler").run(request)
    if result.exit_code != 0:
        raise AgentInvocationError(
            f"plan reconciler provider {result.provider!r} exited with {result.exit_code}"
        )
    path = store.write_plan_version(_raw_text(result.output))
    store.append_event(
        {
            "event": "plan_reconciled",
            "version": path.name,
            "finding_ids": [finding.id for finding in findings],
            "dispositions": [
                {
                    "finding_id": finding.id,
                    "disposition": "submitted_for_independent_re_review",
                }
                for finding in findings
            ],
        }
    )
    store.write_json_artifact(
        f"planning/reconciliation-{path.stem}.json",
        {
            "plan_version": path.name,
            "dispositions": [
                {
                    "finding_id": finding.id,
                    "disposition": "submitted_for_independent_re_review",
                }
                for finding in findings
            ],
        },
        immutable=True,
    )
    return path


def execute(
    registry: RoleRegistry,
    approved_plan: str,
    invariants: list[ContextRef],
    references: list[ContextRef],
    worktree: Path,
    store: RunStore,
    profile_constraints: list[ContextRef] | None = None,
    scope_fence: ContextRef | None = None,
) -> AgentResult:
    packet = assemble(
        "execution",
        [
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            *invariants,
            *references,
            *(profile_constraints or []),
            *([scope_fence] if scope_fence is not None else []),
            ContextRef(label=Category.WORKTREE, path=None, content=str(worktree)),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact("execution/worker-result.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(
            f"implementation worker provider {result.provider!r} exited with {result.exit_code}"
        )
    store.append_event({"event": "executed", "exit_code": result.exit_code})
    return result


MAX_REMEDIATION_CYCLES = 2


class RemediationExhausted(RuntimeError):
    """Blocking findings survived the bounded remediation budget."""


def review_implementation(
    registry: RoleRegistry,
    approved_plan: str,
    diff: str,
    validation_evidence: str,
    profile_constraints: list[ContextRef],
    store: RunStore,
    worktree: Path | None = None,
) -> ReviewResult:
    packet = assemble(
        "implementation_review",
        [
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            ContextRef(label=Category.DIFF, path=None, content=diff),
            ContextRef(
                label=Category.VALIDATION_EVIDENCE,
                path=None,
                content=validation_evidence,
            ),
            *profile_constraints,
        ],
    )
    request = _request(
        registry,
        "implementation_reviewer",
        store,
        packet,
        worktree or store.repo_root,
    )
    raw = invoke_structured(
        registry.adapter_for("implementation_reviewer"),
        request,
        ReviewResult,
        store.root / "schemas",
    )
    result = _renumber(raw, store)
    store.write_versioned_json("implementation-review", result.model_dump(mode="json"))
    store.append_event(
        {
            "event": "implementation_reviewed",
            "outcome": str(result.outcome),
            "finding_ids": [finding.id for finding in result.findings],
        }
    )
    return result


def remediate(
    registry: RoleRegistry,
    blocking: list[Finding],
    approved_plan: str,
    current_state: str,
    worktree: Path,
    store: RunStore,
    cycle: int = 1,
) -> AgentResult:
    if cycle > MAX_REMEDIATION_CYCLES:
        ids = [finding.id for finding in blocking]
        store.append_event({"event": "remediation_exhausted", "finding_ids": ids})
        raise RemediationExhausted(
            f"blocking findings {ids} survived {MAX_REMEDIATION_CYCLES} remediation cycles"
        )
    packet = assemble(
        "remediation",
        [
            ContextRef(
                label=Category.BLOCKING_FINDINGS,
                path=None,
                content="\n".join(
                    f"{finding.id}: {finding.summary}\n"
                    f"  required evidence: {finding.evidence_required}"
                    for finding in blocking
                ),
            ),
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            ContextRef(label=Category.CURRENT_STATE, path=None, content=current_state),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact(f"execution/remediation-v{cycle}.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(
            f"remediation worker provider {result.provider!r} exited with {result.exit_code}"
        )
    store.append_event(
        {"event": "remediated", "cycle": cycle, "finding_ids": [f.id for f in blocking]}
    )
    return result


def verify(
    registry: RoleRegistry,
    request_text: str,
    approved_plan: str,
    criteria: list[str],
    current_state: str,
    validation_evidence: str,
    unresolved: list[str],
    store: RunStore,
    worktree: Path | None = None,
) -> Verification:
    packet = assemble(
        "final_verification",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            ContextRef(
                label=Category.ACCEPTANCE_CRITERIA,
                path=None,
                content="\n".join(criteria),
            ),
            ContextRef(label=Category.CURRENT_STATE, path=None, content=current_state),
            ContextRef(
                label=Category.VALIDATION_EVIDENCE,
                path=None,
                content=validation_evidence,
            ),
            ContextRef(
                label=Category.UNRESOLVED_FINDINGS,
                path=None,
                content="\n".join(unresolved),
            ),
        ],
    )
    request = _request(registry, "verifier", store, packet, worktree or store.repo_root)
    result = invoke_structured(
        registry.adapter_for("verifier"), request, Verification, store.root / "schemas"
    )
    _record_result(store, "verification/final-verification.json", result.model_dump(mode="json"))
    store.append_event(
        {
            "event": "verified",
            "outcome": str(result.outcome),
            "verdicts": [
                {"criterion": verdict.criterion, "verdict": verdict.verdict}
                for verdict in result.verdicts
            ],
            "unresolved_finding_ids": result.unresolved_finding_ids,
        }
    )
    return result
