from pathlib import Path

import pytest

from dev_orchestration.adapters.base import FakeAdapter
from dev_orchestration.adapters.claude import ClaudeAdapter
from dev_orchestration.adapters.codex import MODEL_ALIASES, CodexAdapter
from dev_orchestration.adapters.registry import (
    DEFAULT_ROLES,
    READ_ONLY_ROLES,
    ReadOnlyRoleUnsupportedError,
    RoleRegistry,
    UnknownRoleError,
)
from dev_orchestration.config.models import GlobalConfig, ProjectConfig, RoleConfig


@pytest.fixture
def registry():
    return RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={
            "codex": FakeAdapter(capabilities={"exec", "read_only_review"}),
            "claude": FakeAdapter(),
        },
    )


def test_default_roles_match_the_specification():
    expected = {
        "classifier": ("codex", "luna", "high"),
        "planner": ("claude", "claude-opus-5-5", "medium"),
        "plan_reviewer": ("codex", "sol", "high"),
        "plan_reconciler": ("claude", "claude-sonnet-5-5", "high"),
        "implementation_worker": ("codex", "luna", "high"),
        "implementation_reviewer": ("claude", "claude-opus-5-5", "high"),
        "verifier": ("codex", "luna", "high"),
    }
    assert {
        role: (binding.adapter, binding.model, binding.reasoning)
        for role, binding in DEFAULT_ROLES.items()
    } == expected
    assert DEFAULT_ROLES["classifier"].timeout_seconds == 600
    assert DEFAULT_ROLES["implementation_worker"].timeout_seconds == 1800


def test_a_read_only_role_cannot_be_bound_to_an_adapter_that_ignores_the_restriction():
    registry = RoleRegistry(
        roles={"verifier": RoleConfig(adapter="codex", model="sol")},
        adapters={"codex": FakeAdapter(capabilities={"exec"})},
    )
    with pytest.raises(ReadOnlyRoleUnsupportedError) as excinfo:
        registry.build_request("verifier", prompt="verify", cwd=Path("/w"))
    message = str(excinfo.value)
    assert "verifier" in message
    assert "codex" in message


def test_every_read_only_role_default_binding_can_honour_the_restriction():
    # Guards the whole set, not just verifier: adding a role to READ_ONLY_ROLES
    # while binding it to an adapter that drops allowed_tools fails here.
    adapters = {
        "codex": FakeAdapter(capabilities={"exec", "read_only_review"}),
        "claude": FakeAdapter(),
    }
    registry = RoleRegistry(roles=DEFAULT_ROLES, adapters=adapters)
    for role in READ_ONLY_ROLES:
        request = registry.build_request(role, prompt="x", cwd=Path("/w"))
        assert request.allowed_tools == ["Read", "Grep", "Glob"], role


def test_the_read_only_restriction_survives_into_the_emitted_command():
    codex = CodexAdapter(binary=Path("/bin/codex"))
    codex._capabilities = {"exec": True, "read_only_review": True}
    registry = RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={"codex": codex, "claude": ClaudeAdapter()},
    )
    for role in READ_ONLY_ROLES:
        request = registry.build_request(role, prompt="read", cwd=Path("/w"))
        if DEFAULT_ROLES[role].adapter == "codex":
            argv = codex.build_exec_command(request)
            assert argv[argv.index("-s") + 1] == "read-only"
            expected_model = MODEL_ALIASES.get(DEFAULT_ROLES[role].model, DEFAULT_ROLES[role].model)
            assert argv[argv.index("-m") + 1] == expected_model
        else:
            argv = ClaudeAdapter().build_command(request)
            assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
            assert argv[argv.index("--model") + 1] == DEFAULT_ROLES[role].model
            assert argv[argv.index("--effort") + 1] == DEFAULT_ROLES[role].reasoning
    worker = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    argv = codex.build_exec_command(worker)
    assert argv[argv.index("-s") + 1] == "workspace-write"


def test_build_request_carries_the_configured_model(registry):
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.model_alias == "luna"
    assert request.role == "implementation_worker"


def test_build_request_carries_the_configured_role_timeout():
    adapter = FakeAdapter()
    registry = RoleRegistry(
        roles={"implementation_worker": RoleConfig(adapter="fake", timeout_seconds=7)},
        adapters={"fake": adapter},
    )
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.timeout_seconds == 7


def test_review_roles_are_restricted_to_read_only_tools(registry):
    request = registry.build_request("plan_reviewer", prompt="review", cwd=Path("/w"))
    assert request.allowed_tools == ["Read", "Grep", "Glob"]


def test_worker_roles_are_not_tool_restricted(registry):
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.allowed_tools is None


def test_unknown_role_raises(registry):
    with pytest.raises(UnknownRoleError):
        registry.adapter_for("nonexistent_role")


def test_verifier_role_is_restricted_to_read_only_tools(registry):
    request = registry.build_request("verifier", prompt="verify", cwd=Path("/w"))
    assert request.allowed_tools == ["Read", "Grep", "Glob"]


def test_adapter_for_returns_the_adapter_bound_to_the_role(registry):
    assert registry.adapter_for("implementation_worker") is registry.adapters["codex"]
    assert registry.adapter_for("plan_reviewer") is registry.adapters["codex"]


def test_build_request_carries_reasoning_from_the_binding(registry):
    request = registry.build_request("implementation_worker", prompt="plan", cwd=Path("/w"))
    assert request.reasoning == "high"


def test_build_request_carries_claude_effort(registry):
    request = registry.build_request("planner", prompt="plan", cwd=Path("/w"))
    assert request.reasoning == "medium"


def test_project_roles_override_global_which_override_framework_defaults():
    from dev_orchestration.adapters.registry import default_registry

    project = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "class": "internal_utility"},
            "roles": {"planner": {"adapter": "claude", "model": "opus"}},
        }
    )
    global_config = GlobalConfig(
        roles={
            "planner": RoleConfig(adapter="codex", model="luna"),
            "classifier": RoleConfig(adapter="claude", model="opus"),
        }
    )
    registry = default_registry(project, global_config)
    assert registry.binding_for("planner").adapter == "claude"
    assert registry.binding_for("planner").model == "opus"
    assert registry.binding_for("classifier").adapter == "claude"
    assert registry.binding_for("plan_reconciler").adapter == "claude"


def test_default_registry_without_configuration_uses_framework_defaults():
    from dev_orchestration.adapters.registry import default_registry

    registry = default_registry()
    assert registry.roles == DEFAULT_ROLES
