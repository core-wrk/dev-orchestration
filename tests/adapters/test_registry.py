from pathlib import Path

import pytest

from dev_orchestration.adapters.base import FakeAdapter
from dev_orchestration.adapters.claude import ClaudeAdapter
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
    # The codex double declares only "exec", matching the real CodexAdapter,
    # which builds its argv from a fixed sandbox mode and never reads
    # allowed_tools. A double that claimed read_only_review would let an
    # unenforceable binding pass every test in this file.
    return RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={
            "codex": FakeAdapter(capabilities={"exec"}),
            "claude": FakeAdapter(),
        },
    )


def test_default_roles_match_the_specification():
    assert DEFAULT_ROLES["implementation_worker"].adapter == "codex"
    assert DEFAULT_ROLES["implementation_worker"].model == "luna"
    assert DEFAULT_ROLES["plan_reviewer"].adapter == "claude"
    assert DEFAULT_ROLES["implementation_reviewer"].adapter == "claude"
    # classifier, planner, plan_reconciler, and verifier are all bound to
    # claude, NOT to codex, despite CONFIGURATION.md section 2's illustration.
    # Each is in READ_ONLY_ROLES: its job is to read the worktree and return
    # text (a tier, a plan, a review), never to change anything, and codex
    # cannot honour that restriction -- build_exec_command ignores
    # allowed_tools and always emits -s workspace-write regardless of role.
    # Binding any of them to codex would give it real write access to the
    # worktree it is only supposed to be reading.
    for role in ("classifier", "planner", "plan_reconciler", "verifier"):
        assert DEFAULT_ROLES[role].adapter == "claude", role
        assert role in READ_ONLY_ROLES


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
    adapters = {"codex": FakeAdapter(capabilities={"exec"}), "claude": FakeAdapter()}
    registry = RoleRegistry(roles=DEFAULT_ROLES, adapters=adapters)
    for role in READ_ONLY_ROLES:
        request = registry.build_request(role, prompt="x", cwd=Path("/w"))
        assert request.allowed_tools == ["Read", "Grep", "Glob"], role


def test_the_read_only_restriction_survives_into_the_emitted_command():
    # The join the per-task reviews could not see: registry tests asserted on
    # the request object, adapter tests asserted on the argv, and nothing
    # checked that the restriction actually crossed the boundary between them.
    registry = RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={"codex": FakeAdapter(capabilities={"exec"}), "claude": ClaudeAdapter()},
    )
    request = registry.build_request("verifier", prompt="verify", cwd=Path("/w"))
    argv = ClaudeAdapter().build_command(request)
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"


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
    assert registry.adapter_for("plan_reviewer") is registry.adapters["claude"]


def test_build_request_carries_reasoning_from_the_binding(registry):
    request = registry.build_request("implementation_worker", prompt="plan", cwd=Path("/w"))
    assert request.reasoning == "high"


def test_build_request_reasoning_is_none_when_not_configured(registry):
    request = registry.build_request("plan_reviewer", prompt="review", cwd=Path("/w"))
    assert request.reasoning is None


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
