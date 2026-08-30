import re
import sys
from pathlib import Path

import pytest

from dev_orchestration.context.assembler import (
    PROMPT_BUDGET_BYTES,
    Category,
    ContextContractError,
    assemble,
)
from dev_orchestration.context.packet import ContextRef

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
FENCE_API_FLOOR = (3, 13)


def ref(category, content):
    return ContextRef(label=category, path=None, content=content)


def test_declared_python_floor_supports_the_fence_glob_api():
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', PYPROJECT.read_text())
    assert match
    assert (int(match.group(1)), int(match.group(2))) >= FENCE_API_FLOOR


def test_the_running_interpreter_provides_the_fence_glob_api():
    from pathlib import PurePosixPath

    assert hasattr(PurePosixPath("a"), "full_match")
    assert sys.version_info[:2] >= FENCE_API_FLOOR


def test_fence_glob_patterns_work_on_this_interpreter():
    from dev_orchestration.scope import ScopeFence

    fence = ScopeFence(include=["src/*.py"], exclude=[])
    assert fence.allows("src/app.py")
    assert not fence.allows("src/nested/app.py")


def test_an_oversized_diff_is_truncated_and_the_packet_says_so():
    packet = assemble(
        "implementation_review",
        [
            ref(Category.APPROVED_PLAN, "the plan"),
            ref(Category.DIFF, "x" * (PROMPT_BUDGET_BYTES + 50_000)),
            ref(Category.VALIDATION_EVIDENCE, "unit: exit 0"),
        ],
    )
    assert packet.total_bytes() <= PROMPT_BUDGET_BYTES
    assert Category.CONTEXT_NOTES in packet.labels()
    assert "truncated" in packet.render()
    assert "the plan" in packet.render()


def test_a_packet_within_budget_is_untouched():
    packet = assemble(
        "implementation_review",
        [ref(Category.APPROVED_PLAN, "the plan"), ref(Category.DIFF, "small diff")],
    )
    assert Category.CONTEXT_NOTES not in packet.labels()
    assert "small diff" in packet.render()


def test_oversized_non_truncatable_context_fails_closed():
    with pytest.raises(ContextContractError) as error:
        assemble(
            "implementation_review",
            [ref(Category.APPROVED_PLAN, "p" * (PROMPT_BUDGET_BYTES + 1))],
        )
    assert "budget" in str(error.value)
