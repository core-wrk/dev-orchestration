"""Fail-closed context contracts and safe reference loading."""

from enum import StrEnum
from pathlib import Path

from dev_orchestration.context.packet import ContextPacket, ContextRef
from dev_orchestration.scope import ScopeFence


class ContextContractError(RuntimeError):
    """A stage was offered context outside its declared contract."""


class Category(StrEnum):
    REQUEST = "request"
    REPO_CLASS = "repo_class"
    BRIEF = "brief"
    INVARIANTS = "invariants"
    REFERENCES = "references"
    PLAN = "plan"
    PROPOSED_PLAN = "proposed_plan"
    APPROVED_PLAN = "approved_plan"
    REVIEW_FINDINGS = "review_findings"
    BLOCKING_FINDINGS = "blocking_findings"
    PROFILE_CONSTRAINTS = "profile_constraints"
    SCOPE_FENCE = "scope_fence"
    APPROVAL_SUMMARY = "approval_summary"
    COMMANDS = "commands"
    WORKTREE = "worktree"
    DIFF = "diff"
    VALIDATION_EVIDENCE = "validation_evidence"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    CURRENT_STATE = "current_state"
    UNRESOLVED_FINDINGS = "unresolved_findings"
    BUILDER_NARRATION = "builder_narration"


STAGE_CONTRACTS: dict[str, frozenset[Category]] = {
    "classification": frozenset({Category.REQUEST, Category.REPO_CLASS}),
    "planning": frozenset({Category.BRIEF, Category.INVARIANTS, Category.REFERENCES}),
    "plan_review": frozenset(
        {Category.BRIEF, Category.PROPOSED_PLAN, Category.PROFILE_CONSTRAINTS}
    ),
    "reconciliation": frozenset({Category.PROPOSED_PLAN, Category.REVIEW_FINDINGS}),
    "human_approval": frozenset({Category.APPROVAL_SUMMARY}),
    "execution": frozenset(
        {
            Category.APPROVED_PLAN,
            Category.INVARIANTS,
            Category.REFERENCES,
            Category.PROFILE_CONSTRAINTS,
            Category.SCOPE_FENCE,
            Category.WORKTREE,
        }
    ),
    "validation": frozenset({Category.COMMANDS, Category.WORKTREE}),
    "implementation_review": frozenset(
        {
            Category.APPROVED_PLAN,
            Category.DIFF,
            Category.VALIDATION_EVIDENCE,
            Category.PROFILE_CONSTRAINTS,
        }
    ),
    "remediation": frozenset(
        {Category.BLOCKING_FINDINGS, Category.APPROVED_PLAN, Category.CURRENT_STATE}
    ),
    "final_verification": frozenset(
        {
            Category.REQUEST,
            Category.APPROVED_PLAN,
            Category.ACCEPTANCE_CRITERIA,
            Category.CURRENT_STATE,
            Category.VALIDATION_EVIDENCE,
            Category.UNRESOLVED_FINDINGS,
        }
    ),
}


def assemble(
    stage: str,
    offered: list[ContextRef],
    fence: ScopeFence | None = None,
) -> ContextPacket:
    del fence
    allowed = STAGE_CONTRACTS.get(stage)
    if allowed is None:
        raise ContextContractError(
            f"unknown stage {stage!r}; declared stages are {sorted(STAGE_CONTRACTS)}"
        )
    for item in offered:
        try:
            category = Category(item.label)
        except ValueError as exc:
            raise ContextContractError(f"unknown context category {item.label!r}") from exc
        if category not in allowed:
            raise ContextContractError(
                f"stage {stage!r} may not receive category {category!r}; "
                f"its contract allows {sorted(allowed)}"
            )
    return ContextPacket(stage=stage, items=list(offered))


def read_reference(repo_root: Path, rel_path: str, fence: ScopeFence) -> ContextRef:
    """Read only a regular file whose resolved path stays inside the fence."""
    if not fence.allows(rel_path):
        raise ContextContractError(
            f"{rel_path} is outside the scope fence and must not enter a prompt"
        )
    root = repo_root.resolve()
    target = (repo_root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ContextContractError(
            f"{rel_path} resolves outside repository root and must not enter a prompt"
        ) from exc
    if not target.is_file():
        raise ContextContractError(f"{rel_path} is not a readable file")
    resolved_rel = target.relative_to(root).as_posix()
    if not fence.allows(resolved_rel):
        raise ContextContractError(
            f"{rel_path} resolves to excluded path {resolved_rel} and must not enter a prompt"
        )
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContextContractError(f"could not read reference {rel_path}: {exc}") from exc
    return ContextRef(label=Category.REFERENCES, path=rel_path, content=content)
