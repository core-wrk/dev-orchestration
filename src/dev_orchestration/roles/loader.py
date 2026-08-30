"""Role prompts loaded from reviewable package data files."""

from pathlib import Path

ROLE_TEMPLATES = frozenset(
    {
        "classifier",
        "planner",
        "plan_reviewer",
        "plan_reconciler",
        "implementation_worker",
        "implementation_reviewer",
        "verifier",
    }
)

_DIRECTORY = Path(__file__).parent


class MissingTemplateError(FileNotFoundError):
    """A declared role has no prompt template."""


def load_role_prompt(role: str) -> str:
    if role not in ROLE_TEMPLATES:
        raise MissingTemplateError(
            f"{role!r} is not a declared role; declared roles are {sorted(ROLE_TEMPLATES)}"
        )
    path = _DIRECTORY / f"{role}.md"
    if not path.is_file():
        raise MissingTemplateError(f"{path} does not exist")
    return path.read_text(encoding="utf-8")
