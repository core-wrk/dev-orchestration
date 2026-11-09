"""Provider-neutral workflow stages."""

import difflib
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

from dev_orchestration.adapters.base import (
    AgentResult,
    AgentTimeoutError,
    agent_result_record,
    provider_failure_reason,
)
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.context.assembler import Category, assemble
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import ProjectClass
from dev_orchestration.domain.findings import (
    Classification,
    DependencyReport,
    Finding,
    ReviewResult,
    Verification,
    VerifierAnswer,
    criterion_ids,
    next_finding_id,
)
from dev_orchestration.domain.run import MAX_REMEDIATION_CYCLES
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.workflow.contract import contract_context, repair_context
from dev_orchestration.workflow.dependencies import render_dependency_context
from dev_orchestration.workflow.invoke import AgentInvocationError, invoke_structured
from dev_orchestration.workflow.retry_feedback import context_ref as prior_failures_context
from dev_orchestration.workflow.validation import ValidationOutcome


def _request(registry: RoleRegistry, role: str, store: RunStore, packet, cwd: Path):
    return registry.build_request(
        role,
        prompt=load_role_prompt(role),
        cwd=cwd,
        context=packet,
        on_process_started=lambda pid: store.record_active_provider(
            role, registry.binding_for(role).adapter, pid
        ),
        on_process_finished=store.clear_active_provider,
    )


def _prior_failures(store: RunStore) -> list[ContextRef]:
    item = prior_failures_context(store)
    return [item] if item is not None else []


def _raw_text(output: dict | str) -> str:
    return output if isinstance(output, str) else json.dumps(output, indent=2) + "\n"


def _criteria_context(criteria: list[str]) -> ContextRef:
    return ContextRef(
        label=Category.ACCEPTANCE_CRITERIA,
        path=None,
        content=json.dumps(criteria, ensure_ascii=False),
    )


def _dependency_guidance(report: str) -> str:
    try:
        return render_dependency_context(json.loads(report))
    except (ValueError, TypeError) as exc:
        if "24,000 UTF-8 bytes" in str(exc):
            raise AgentInvocationError(str(exc)) from exc
        raise AgentInvocationError("saved dependency report is invalid") from exc


def _contract_items(contract: dict | None, repair: dict | None) -> list[ContextRef]:
    """Stable instructions first; the changing repair evidence goes after them."""
    return [
        *([contract_context(contract)] if contract is not None else []),
        *([repair_context(repair)] if repair is not None else []),
    ]


def _agent_result_json(result: AgentResult) -> dict:
    return agent_result_record(result)


def _record_result(store: RunStore, name: str, value: object) -> None:
    store.write_json_artifact(name, value)


def classify(
    registry: RoleRegistry,
    request_text: str,
    project_class: ProjectClass,
    store: RunStore,
    available_profiles: list[str] | None = None,
    references: list[ContextRef] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> Classification:
    profiles = available_profiles or []
    repo_context = f"{project_class}\nAvailable profiles: {', '.join(profiles) or '(none)'}"
    packet = assemble(
        "classification",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            ContextRef(label=Category.REPO_CLASS, path=None, content=repo_context),
            *(references or []),
            ContextRef(
                label=Category.ACCEPTANCE_CRITERIA,
                path=None,
                content=json.dumps(acceptance_criteria or [], ensure_ascii=False),
            ),
        ],
    )
    request = _request(registry, "classifier", store, packet, store.repo_root)
    adapter = registry.adapter_for("classifier")
    codex_classifier = registry.binding_for("classifier").adapter == "codex"
    if codex_classifier:
        first_timeout = min(request.timeout_seconds or 60, 60)
        first_request = replace(request, timeout_seconds=first_timeout)
        try:
            result = invoke_structured(
                adapter, first_request, Classification, store.root / "schemas"
            )
        except AgentTimeoutError as exc:
            store.append_event(
                {
                    "event": "classifier_attempt_failed",
                    "attempt": 1,
                    "timeout_seconds": exc.timeout_seconds,
                }
            )
            try:
                result = invoke_structured(adapter, request, Classification, store.root / "schemas")
            except AgentTimeoutError as retry_exc:
                store.append_event(
                    {
                        "event": "classifier_attempt_failed",
                        "attempt": 2,
                        "timeout_seconds": retry_exc.timeout_seconds,
                    }
                )
                raise
            store.append_event(
                {
                    "event": "classifier_attempt_succeeded",
                    "attempt": 2,
                    "timeout_seconds": request.timeout_seconds,
                }
            )
        else:
            store.append_event(
                {
                    "event": "classifier_attempt_succeeded",
                    "attempt": 1,
                    "timeout_seconds": first_timeout,
                }
            )
    else:
        result = invoke_structured(
            adapter,
            request,
            Classification,
            store.root / "schemas",
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


def discover_dependencies(
    registry: RoleRegistry,
    request_text: str,
    inventory: str,
    metadata: dict,
    invariants: list[ContextRef],
    store: RunStore,
) -> str:
    packet = assemble(
        "dependency_discovery",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            *invariants,
            ContextRef(
                label=Category.REFERENCES, path="dependency-source-inventory", content=inventory
            ),
        ],
    )
    request = _request(registry, "dependency_analyst", store, packet, store.repo_root)
    report = invoke_structured(
        registry.adapter_for("dependency_analyst"),
        request,
        DependencyReport,
        store.root / "schemas",
    )
    supplied = metadata.get("supplied_excerpts", {})
    eligible = set(supplied)
    for finding in report.findings:
        excerpt = finding.excerpt.strip()
        if finding.source not in eligible or not excerpt or excerpt not in supplied[finding.source]:
            raise AgentInvocationError(
                f"dependency analyst cited unsupported evidence from {finding.source!r}"
            )
    report = report.model_copy(
        update={
            "searched_sources": [source["path"] for source in metadata["prompt_sources"]],
            "inaccessible_sources": list(
                dict.fromkeys(metadata["inaccessible_sources"] + report.inaccessible_sources)
            ),
            "budget_exhausted": metadata["budget_exhausted"],
            "incomplete_searches": report.incomplete_searches
            + (["source inventory byte budget exhausted"] if metadata["budget_exhausted"] else []),
        }
    )
    artifact = {
        **report.model_dump(mode="json"),
        "source_identities": metadata["sources"],
        "declared_sources": metadata["declared_sources"],
        "inaccessible_sources": list(
            dict.fromkeys(metadata["inaccessible_sources"] + report.inaccessible_sources)
        ),
        "source_inventory_identity": hashlib.sha256(
            json.dumps(
                {
                    "declared_sources": metadata["declared_sources"],
                    "sources": metadata["sources"],
                    "inaccessible_sources": metadata["inaccessible_sources"],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "inventory_budget_bytes": metadata["budget_bytes"],
        "inventory_consumed_bytes": metadata["consumed_bytes"],
    }
    store.write_json_artifact("dependency-report.json", artifact, immutable=True)
    render_dependency_context(artifact)
    return json.dumps(artifact, ensure_ascii=False)


def plan(
    registry: RoleRegistry,
    brief: str,
    invariants: list[ContextRef],
    references: list[ContextRef],
    store: RunStore,
    scope_fence: ContextRef | None = None,
    acceptance_criteria: list[str] | None = None,
    dependency_report: str = "",
) -> Path:
    packet = assemble(
        "planning",
        [
            ContextRef(label=Category.BRIEF, path=None, content=brief),
            ContextRef(label=Category.REQUEST, path=None, content=brief),
            _criteria_context(acceptance_criteria or []),
            *invariants,
            *references,
            *([scope_fence] if scope_fence is not None else []),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "planner", store, packet, store.repo_root)
    result = registry.adapter_for("planner").run(request)
    store.write_json_artifact("execution/planner-result.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("planner", result))
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
    acceptance_criteria: list[str] | None = None,
    scope_fence: ContextRef | None = None,
    dependency_report: str = "",
) -> ReviewResult:
    packet = assemble(
        "plan_review",
        [
            ContextRef(label=Category.BRIEF, path=None, content=brief),
            ContextRef(label=Category.REQUEST, path=None, content=brief),
            _criteria_context(acceptance_criteria or []),
            ContextRef(label=Category.PROPOSED_PLAN, path=None, content=plan_text),
            *profile_constraints,
            *([scope_fence] if scope_fence is not None else []),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "plan_reviewer", store, packet, store.repo_root)
    raw = invoke_structured(
        registry.adapter_for("plan_reviewer"),
        request,
        ReviewResult,
        store.root / "schemas",
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


def _evidence_line(finding: Finding) -> str:
    """Render the evidence demand only when the finding actually carries one."""
    if finding.evidence_required and finding.evidence_required.strip():
        return f"\n  required evidence: {finding.evidence_required}"
    return ""


def reconcile(
    registry: RoleRegistry,
    plan_text: str,
    findings: list[Finding],
    store: RunStore,
    request_text: str = "",
    acceptance_criteria: list[str] | None = None,
    scope_fence: ContextRef | None = None,
    dependency_report: str = "",
    *,
    cycle: int = 1,
) -> Path:
    packet = assemble(
        "reconciliation",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            _criteria_context(acceptance_criteria or []),
            *([scope_fence] if scope_fence is not None else []),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
            ContextRef(label=Category.PROPOSED_PLAN, path=None, content=plan_text),
            ContextRef(
                label=Category.REVIEW_FINDINGS,
                path=None,
                content="\n".join(
                    f"{finding.id} [{finding.severity}] {finding.summary}{_evidence_line(finding)}"
                    for finding in findings
                ),
            ),
        ],
    )
    request = _request(registry, "plan_reconciler", store, packet, store.repo_root)
    store.record_repair_round("plan_review", cycle)
    result = registry.adapter_for("plan_reconciler").run(request)
    store.write_json_artifact("execution/reconciler-result.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("plan reconciler", result))
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
    dependency_report: str = "",
    contract: dict | None = None,
) -> AgentResult:
    packet = assemble(
        "execution",
        [
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            *_contract_items(contract, None),
            *invariants,
            *references,
            *(profile_constraints or []),
            *([scope_fence] if scope_fence is not None else []),
            ContextRef(label=Category.WORKTREE, path=None, content=str(worktree)),
            *_prior_failures(store),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact("execution/worker-result.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("implementation worker", result))
    store.append_event({"event": "executed", "exit_code": result.exit_code})
    return result


class RemediationExhausted(RuntimeError):
    """Blocking findings survived the bounded remediation budget."""

    def __init__(self, message: str, cause: str = "implementation_review_exhausted") -> None:
        super().__init__(message)
        self.cause = cause


def review_implementation(
    registry: RoleRegistry,
    approved_plan: str,
    diff: str,
    validation_evidence: str,
    profile_constraints: list[ContextRef],
    store: RunStore,
    worktree: Path | None = None,
    dependency_report: str = "",
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
            *_prior_failures(store),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
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
    dependency_report: str = "",
    contract: dict | None = None,
    repair: dict | None = None,
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
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            *_contract_items(contract, repair),
            ContextRef(
                label=Category.BLOCKING_FINDINGS,
                path=None,
                content="\n".join(
                    f"{finding.id}: {finding.summary}{_evidence_line(finding)}"
                    for finding in blocking
                ),
            ),
            ContextRef(label=Category.CURRENT_STATE, path=None, content=current_state),
            *_prior_failures(store),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    store.record_repair_round("implementation_review", cycle)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact(f"execution/remediation-v{cycle}.json", _agent_result_json(result))
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("remediation worker", result))
    store.append_event(
        {"event": "remediated", "cycle": cycle, "finding_ids": [f.id for f in blocking]}
    )
    return result


def repair_validation(
    registry: RoleRegistry,
    approved_plan: str,
    diff: str,
    outcomes: list[ValidationOutcome],
    worktree: Path,
    store: RunStore,
    dependency_report: str = "",
    contract: dict | None = None,
    repair: dict | None = None,
    round_number: int = 1,
) -> AgentResult:
    """Give the worker the failed command evidence for one validation repair round."""
    failed = [outcome for outcome in outcomes if not outcome.passed]
    evidence = "\n\n".join(
        f"{outcome.name}: exit {outcome.exit_code}\n"
        f"stdout:\n{outcome.stdout_tail}\n"
        f"stderr:\n{outcome.stderr_tail}"
        for outcome in failed
    )
    packet = assemble(
        "remediation",
        [
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            *_contract_items(contract, repair),
            ContextRef(label=Category.DIFF, path=None, content=diff),
            ContextRef(label=Category.VALIDATION_EVIDENCE, path=None, content=evidence),
            *_prior_failures(store),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    store.record_repair_round("validation", round_number)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact(
        f"execution/validation-repair-v{round_number}.json",
        _agent_result_json(result),
        immutable=True,
    )
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("validation repair worker", result))
    store.append_event(
        {
            "event": "validation_repaired",
            "failed_commands": [outcome.name for outcome in failed],
        }
    )
    return result


def repair_verification(
    registry: RoleRegistry,
    approved_plan: str,
    verification: Verification,
    diff: str,
    validation_evidence: str,
    worktree: Path,
    store: RunStore,
    dependency_report: str = "",
    contract: dict | None = None,
    repair: dict | None = None,
    round_number: int = 1,
) -> AgentResult:
    """Give the worker the rejected acceptance checks for one verification repair round."""
    rejected = [verdict for verdict in verification.verdicts if verdict.verdict != "PASS"]
    mode = repair.get("mode") if repair is not None else None
    if mode == "findings":
        blocking = "\n".join(
            [
                (
                    "Unresolved review findings: final verification passed every acceptance "
                    "criterion but will not accept the change while these review findings "
                    "stand. Resolve each one, changing only the files the repair scope lists."
                ),
                f"{repair['check']}: {repair['expected']}",
                *(f"{item.finding_id}: {item.note}" for item in verification.outcome_explanation),
            ]
        )
    else:
        evidence_note = (
            "Evidence repair: final verification could not see these criteria demonstrated. "
            "Add or extend tests that directly demonstrate each one; change product code only "
            "where such a test exposes a defect, inside the approved scope.\n"
            if mode == "evidence"
            else ""
        )
        blocking = evidence_note + (
            "\n".join(
                f"{verdict.criterion}: {verdict.verdict}: {verdict.evidence}"
                for verdict in rejected
            )
            or f"final verification returned {verification.outcome}"
        )
    packet = assemble(
        "remediation",
        [
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            *_contract_items(contract, repair),
            ContextRef(
                label=Category.BLOCKING_FINDINGS,
                path=None,
                content=blocking,
            ),
            ContextRef(label=Category.DIFF, path=None, content=diff),
            ContextRef(label=Category.VALIDATION_EVIDENCE, path=None, content=validation_evidence),
            *_prior_failures(store),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "implementation_worker", store, packet, worktree)
    store.record_repair_round("final_verification", round_number)
    result = registry.adapter_for("implementation_worker").run(request)
    store.write_json_artifact(
        f"execution/verification-repair-v{round_number}.json",
        _agent_result_json(result),
        immutable=True,
    )
    if result.exit_code != 0:
        raise AgentInvocationError(provider_failure_reason("verification repair worker", result))
    store.append_event(
        {
            "event": "verification_repaired",
            "round": round_number,
            "rejected_criteria": [verdict.criterion for verdict in rejected],
        }
    )
    return result


def verify(
    registry: RoleRegistry,
    request_text: str,
    approved_plan: str,
    criteria: list[str],
    current_state: str,
    validation_evidence: str,
    findings: list[Finding],
    store: RunStore,
    worktree: Path | None = None,
    dependency_report: str = "",
) -> Verification:
    previous = [
        int(path.stem.rsplit("-v", 1)[1])
        for path in store.root.glob("verification/current-state-diff-v*.patch")
        if path.stem.rsplit("-v", 1)[-1].isdigit()
    ]
    version = max(previous, default=0) + 1
    diff_path = store.write_text_artifact(
        f"verification/current-state-diff-v{version}.patch", current_state, immutable=True
    )
    packet = assemble(
        "final_verification",
        [
            ContextRef(label=Category.REQUEST, path=None, content=request_text),
            ContextRef(label=Category.APPROVED_PLAN, path=None, content=approved_plan),
            ContextRef(
                label=Category.ACCEPTANCE_CRITERIA,
                path=None,
                content="\n".join(
                    f"{cid}: {criterion}"
                    for cid, criterion in zip(criterion_ids(criteria), criteria, strict=True)
                ),
            ),
            ContextRef(
                label=Category.CURRENT_STATE,
                path=str(diff_path),
                content=(
                    f"Complete current-state diff (also provided inline): {diff_path}\n\n"
                    f"{current_state}"
                ),
            ),
            ContextRef(
                label=Category.VALIDATION_EVIDENCE,
                path=None,
                content=validation_evidence,
            ),
            ContextRef(
                label=Category.UNRESOLVED_FINDINGS,
                path=None,
                content=json.dumps(
                    [finding.model_dump(mode="json") for finding in findings],
                    indent=2,
                ),
            ),
            *(
                [
                    ContextRef(
                        label=Category.DEPENDENCY_REPORT,
                        path="dependency-report.json",
                        content=_dependency_guidance(dependency_report),
                    )
                ]
                if dependency_report
                else []
            ),
        ],
    )
    request = _request(registry, "verifier", store, packet, worktree or store.repo_root)
    answer = invoke_structured(
        registry.adapter_for("verifier"),
        request,
        VerifierAnswer,
        store.root / "schemas",
        context={"criterion_ids": criterion_ids(criteria)},
    )
    # Saved with the supplied criteria text, so later gates compare text the verifier never wrote.
    result = answer.to_verification(criteria)
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
            "outcome_explanation": [
                item.model_dump(mode="json") for item in result.outcome_explanation
            ],
        }
    )
    return result
