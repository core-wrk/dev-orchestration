from pathlib import Path

import pytest

from dev_orchestration.adapters.base import FakeAdapter
from dev_orchestration.adapters.registry import (
    DEFAULT_ROLES,
    RoleRegistry,
    UnknownRoleError,
)


@pytest.fixture
def registry():
    return RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={"codex": FakeAdapter(), "claude": FakeAdapter()},
    )


def test_default_roles_match_the_specification():
    assert DEFAULT_ROLES["planner"].adapter == "codex"
    assert DEFAULT_ROLES["planner"].model == "sol"
    assert DEFAULT_ROLES["implementation_worker"].model == "luna"
    assert DEFAULT_ROLES["plan_reviewer"].adapter == "claude"
    assert DEFAULT_ROLES["implementation_reviewer"].adapter == "claude"
    assert DEFAULT_ROLES["verifier"].model == "sol"


def test_build_request_carries_the_configured_model(registry):
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.model_alias == "luna"
    assert request.role == "implementation_worker"


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
    assert registry.adapter_for("planner") is registry.adapters["codex"]
    assert registry.adapter_for("plan_reviewer") is registry.adapters["claude"]
