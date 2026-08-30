from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AgentRequest, compose_prompt
from dev_orchestration.adapters.claude import ClaudeAdapter
from dev_orchestration.adapters.codex import CodexAdapter
from dev_orchestration.context.assembler import PROMPT_BUDGET_BYTES, PromptBudgetError
from dev_orchestration.context.packet import ContextPacket, ContextRef

PACKET = ContextPacket(
    stage="planning", items=[ContextRef(label="invariants", path="AGENTS.md", content="never push")]
)


def request(**kwargs):
    return AgentRequest(role="planner", prompt="do the task", cwd=Path("/w"), **kwargs)


def test_codex_includes_context_before_task():
    command = CodexAdapter(binary=Path("/bin/codex")).build_exec_command(request(context=PACKET))
    assert command[-1].index("never push") < command[-1].index("do the task")
    assert sum("do the task" in part for part in command) == 1


def test_codex_without_context_is_unchanged():
    assert (
        CodexAdapter(binary=Path("/bin/codex")).build_exec_command(request())[-1] == "do the task"
    )


def test_claude_includes_context_in_prompt_argument():
    command = ClaudeAdapter().build_command(request(context=PACKET))
    assert command[command.index("-p") + 1] == command[-1]
    assert "never push" in command[-1]


def test_codex_passes_output_schema():
    schema = Path("/schemas/classification.json")
    command = CodexAdapter(binary=Path("/bin/codex")).build_exec_command(
        request(expected_schema=schema)
    )
    assert command[command.index("--output-schema") + 1] == str(schema)


def test_final_prompt_budget_is_enforced_without_context_overhead_hiding_it():
    with pytest.raises(PromptBudgetError):
        compose_prompt(
            AgentRequest(
                role="planner",
                prompt="x" * (PROMPT_BUDGET_BYTES + 1),
                cwd=Path("/w"),
            )
        )
