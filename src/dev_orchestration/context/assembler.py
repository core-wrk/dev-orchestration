"""Fail-closed context contracts and safe reference loading."""

from enum import StrEnum
from pathlib import Path

from dev_orchestration.context.packet import ContextPacket, ContextRef
from dev_orchestration.scope import ScopeFence


class ContextContractError(RuntimeError):
    """A stage was offered context outside its declared contract."""


class PromptBudgetError(ContextContractError):
    """The final provider prompt cannot fit without dropping contract content."""


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
    CONTEXT_NOTES = "context_notes"


STAGE_CONTRACTS: dict[str, frozenset[Category]] = {
    "classification": frozenset(
        {
            Category.REQUEST,
            Category.REPO_CLASS,
            Category.REFERENCES,
            Category.ACCEPTANCE_CRITERIA,
            Category.CONTEXT_NOTES,
        }
    ),
    "planning": frozenset(
        {Category.BRIEF, Category.INVARIANTS, Category.REFERENCES, Category.CONTEXT_NOTES}
    ),
    "plan_review": frozenset(
        {
            Category.BRIEF,
            Category.PROPOSED_PLAN,
            Category.PROFILE_CONSTRAINTS,
            Category.CONTEXT_NOTES,
        }
    ),
    "reconciliation": frozenset(
        {Category.PROPOSED_PLAN, Category.REVIEW_FINDINGS, Category.CONTEXT_NOTES}
    ),
    "human_approval": frozenset({Category.APPROVAL_SUMMARY, Category.CONTEXT_NOTES}),
    "execution": frozenset(
        {
            Category.APPROVED_PLAN,
            Category.INVARIANTS,
            Category.REFERENCES,
            Category.PROFILE_CONSTRAINTS,
            Category.SCOPE_FENCE,
            Category.WORKTREE,
            Category.CONTEXT_NOTES,
        }
    ),
    "validation": frozenset({Category.COMMANDS, Category.WORKTREE, Category.CONTEXT_NOTES}),
    "implementation_review": frozenset(
        {
            Category.APPROVED_PLAN,
            Category.DIFF,
            Category.VALIDATION_EVIDENCE,
            Category.PROFILE_CONSTRAINTS,
            Category.CONTEXT_NOTES,
        }
    ),
    "remediation": frozenset(
        {
            Category.BLOCKING_FINDINGS,
            Category.APPROVED_PLAN,
            Category.CURRENT_STATE,
            Category.DIFF,
            Category.VALIDATION_EVIDENCE,
            Category.CONTEXT_NOTES,
        }
    ),
    "final_verification": frozenset(
        {
            Category.REQUEST,
            Category.APPROVED_PLAN,
            Category.ACCEPTANCE_CRITERIA,
            Category.CURRENT_STATE,
            Category.VALIDATION_EVIDENCE,
            Category.UNRESOLVED_FINDINGS,
            Category.CONTEXT_NOTES,
        }
    ),
}


# Keep room for role instructions, rendered labels, separators, and explicit
# provenance notes. The adapter boundary enforces the same public budget over
# the complete provider argv element.
PROMPT_BUDGET_BYTES = 96_000
_CONTEXT_BUDGET_BYTES = PROMPT_BUDGET_BYTES - 8_192
_NOTES_RESERVE_BYTES = 2_048
TRUNCATION_MARKER = "\n\n[... truncated to fit the prompt budget ...]"
TRUNCATABLE: frozenset[Category] = frozenset(
    {
        Category.DIFF,
        Category.CURRENT_STATE,
        Category.VALIDATION_EVIDENCE,
        Category.REFERENCES,
    }
)


def _size(item: ContextRef) -> int:
    return len(item.content.encode("utf-8"))


def _render_size(items: list[ContextRef]) -> int:
    return len("\n".join(item.render() for item in items).encode("utf-8"))


def _truncate(content: str, byte_count: int) -> str:
    return content.encode("utf-8")[:byte_count].decode("utf-8", errors="ignore")


def _fit_to_budget(items: list[ContextRef], budget: int) -> tuple[list[ContextRef], list[str]]:
    """Shrink evidence until rendered context fits; never drop a contract."""
    fixed = [item for item in items if Category(item.label) not in TRUNCATABLE]
    if _render_size(fixed) > budget:
        raise PromptBudgetError(
            f"non-truncatable context exceeds the {PROMPT_BUDGET_BYTES}-byte prompt budget"
        )
    fitted = list(items)
    notes: list[str] = []
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    while _render_size(fitted) > budget:
        candidates = [
            (index, _size(item))
            for index, item in enumerate(fitted)
            if Category(item.label) in TRUNCATABLE and _size(item) > 0
        ]
        if not candidates:
            raise PromptBudgetError(
                f"context cannot fit the {PROMPT_BUDGET_BYTES}-byte prompt budget without "
                "dropping a non-truncatable contract"
            )
        index, current_size = max(candidates, key=lambda candidate: candidate[1])
        excess = _render_size(fitted) - budget
        kept_size = max(0, current_size - excess - marker_bytes)
        item = fitted[index]
        fitted[index] = item.model_copy(
            update={"content": _truncate(item.content, kept_size) + TRUNCATION_MARKER}
        )
        notes.append(f"{item.label}: truncated from {current_size} bytes")
    return fitted, notes


def assemble(stage: str, offered: list[ContextRef]) -> ContextPacket:
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
    fitted, notes = _fit_to_budget(list(offered), _CONTEXT_BUDGET_BYTES - _NOTES_RESERVE_BYTES)
    if notes:
        fitted.append(ContextRef(label=Category.CONTEXT_NOTES, path=None, content="\n".join(notes)))
    if _render_size(fitted) > _CONTEXT_BUDGET_BYTES:
        raise PromptBudgetError(
            f"context notes would exceed the {PROMPT_BUDGET_BYTES}-byte prompt budget"
        )
    return ContextPacket(stage=stage, items=fitted)


def read_reference(repo_root: Path, rel_path: str, fence: ScopeFence) -> ContextRef:
    """Read a declared regular file that resolves inside the repository.

    The scope fence limits writes, not declared prompt sources. A project may
    exclude its instruction files from modification while still requiring them
    in every agent prompt.
    """
    del fence
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
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContextContractError(f"could not read reference {rel_path}: {exc}") from exc
    return ContextRef(label=Category.REFERENCES, path=rel_path, content=content)
