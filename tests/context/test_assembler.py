import pytest

from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.context.assembler import (
    STAGE_CONTRACTS,
    Category,
    ContextContractError,
    assemble,
    read_reference,
)
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import resolve_runtime_context


def ref(category, content="x"):
    return ContextRef(label=category, path=None, content=content)


def test_validation_receives_no_agent_output_whatsoever():
    assert STAGE_CONTRACTS["validation"] == frozenset(
        {Category.COMMANDS, Category.WORKTREE, Category.CONTEXT_NOTES}
    )
    assert Category.REVIEW_FINDINGS not in STAGE_CONTRACTS["validation"]


@pytest.mark.parametrize(
    "stage,forbidden",
    [
        ("planning", Category.REVIEW_FINDINGS),
        ("execution", Category.REVIEW_FINDINGS),
        ("validation", Category.PLAN),
    ],
)
def test_a_stage_refuses_a_category_its_contract_excludes(stage, forbidden):
    with pytest.raises(ContextContractError) as error:
        assemble(stage, [ref(forbidden)])
    assert forbidden in str(error.value)


def test_unknown_stage_fails_closed():
    with pytest.raises(ContextContractError):
        assemble("not_a_stage", [])


def test_read_reference_refuses_a_path_outside_the_fence(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "keys.txt").write_text("token")
    with pytest.raises(ContextContractError):
        read_reference(
            tmp_path, "secrets/keys.txt", ScopeFence(include=["**"], exclude=["secrets"])
        )


def test_read_reference_reads_a_path_inside_the_fence(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    item = read_reference(tmp_path, "src/app.py", ScopeFence(include=["**"], exclude=[]))
    assert item.path == "src/app.py"
    assert "print('hi')" in item.content


def test_reference_symlinks_cannot_escape_root_or_fence(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "outside.txt").symlink_to(outside)
    (tmp_path / "src" / "excluded.txt").write_text("secret")
    (tmp_path / "src" / "link-to-excluded.txt").symlink_to(tmp_path / "src" / "excluded.txt")
    fence = ScopeFence(include=["src/"], exclude=["src/excluded.txt"])
    with pytest.raises(ContextContractError):
        read_reference(tmp_path, "src/outside.txt", ScopeFence(include=["**"], exclude=[]))
    with pytest.raises(ContextContractError):
        read_reference(tmp_path, "src/link-to-excluded.txt", fence)
    outside.unlink()


def test_runtime_packet_records_inclusions_exclusions_conflicts_and_scope(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("keep the scope narrow\n")
    (tmp_path / "src" / "policy.md").write_text("security policy\n")
    config_data = {
        "project": {"name": "x", "class": "internal_utility"},
        "profiles": {
            "available": ["auth", "payments"],
            "definitions": {
                "auth": {
                    "controls": {"owner": "security"},
                    "context_refs": ["src/policy.md"],
                },
                "payments": {"controls": {"owner": "finance"}},
            },
        },
        "scope": {"include": ["src/"], "exclude": []},
        "context": {
            "persistent": ["src/AGENTS.md", "src/missing.md"],
            "conflict_policy": "escalate",
        },
    }
    config = ProjectConfig.model_validate(config_data)
    fence = ScopeFence.from_config(config.scope)
    runtime = resolve_runtime_context(tmp_path, config, ["auth", "payments"], fence)
    scope = ContextRef(
        label=Category.SCOPE_FENCE,
        path="config:scope",
        content='{"include": ["src/"], "exclude": []}',
    )
    packet = assemble(
        "execution",
        [
            *runtime.invariants,
            *runtime.references,
            *runtime.profile_constraints,
            scope,
            ref(Category.WORKTREE, str(tmp_path)),
        ],
    )
    assert "src/AGENTS.md" in runtime.included
    assert any(item.startswith("src/missing.md:") for item in runtime.excluded)
    assert runtime.conflicts and "owner" in runtime.conflicts[0]
    assert Category.SCOPE_FENCE in packet.labels()

    fail_closed = ProjectConfig.model_validate(
        {
            **config.model_dump(mode="json"),
            "context": {
                **config.context.model_dump(mode="json"),
                "conflict_policy": "fail_closed",
            },
        }
    )
    with pytest.raises(ContextContractError):
        resolve_runtime_context(tmp_path, fail_closed, ["auth", "payments"], fence)
