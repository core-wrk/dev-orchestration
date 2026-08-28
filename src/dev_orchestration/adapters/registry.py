"""Logical roles resolve to a provider adapter plus a model alias here.

Nowhere else in the codebase should a model name appear.
"""

from pathlib import Path

from dev_orchestration.adapters.base import AgentAdapter, AgentRequest
from dev_orchestration.adapters.claude import READ_ONLY_TOOLS
from dev_orchestration.config.models import RoleConfig

READ_ONLY_ROLES = frozenset({"plan_reviewer", "implementation_reviewer", "verifier"})

DEFAULT_ROLES: dict[str, RoleConfig] = {
    "classifier": RoleConfig(adapter="codex", model="luna", reasoning="medium"),
    "planner": RoleConfig(adapter="codex", model="sol", reasoning="high"),
    "plan_reviewer": RoleConfig(adapter="claude", model="opus"),
    "plan_reconciler": RoleConfig(adapter="codex", model="sol", reasoning="high"),
    "goal_executor": RoleConfig(adapter="codex", model="luna", reasoning="high"),
    "implementation_worker": RoleConfig(adapter="codex", model="luna", reasoning="high"),
    "implementation_reviewer": RoleConfig(adapter="claude", model="opus"),
    # verifier is in READ_ONLY_ROLES, so it must be bound to an adapter that
    # supports read_only_review. codex does not: build_exec_command ignores
    # allowed_tools and always emits -s workspace-write, which would silently
    # give the independent verifier write access to the run worktree.
    "verifier": RoleConfig(adapter="claude", model="opus"),
}


class UnknownRoleError(KeyError):
    """A role was requested that has no configured binding."""


class ReadOnlyRoleUnsupportedError(RuntimeError):
    """A read-only role is bound to an adapter that cannot honour the restriction."""


class RoleRegistry:
    def __init__(self, roles: dict[str, RoleConfig], adapters: dict[str, AgentAdapter]) -> None:
        self.roles = roles
        self.adapters = adapters

    def binding_for(self, role: str) -> RoleConfig:
        try:
            return self.roles[role]
        except KeyError as exc:
            raise UnknownRoleError(f"no binding configured for role {role!r}") from exc

    def adapter_for(self, role: str) -> AgentAdapter:
        binding = self.binding_for(role)
        try:
            return self.adapters[binding.adapter]
        except KeyError as exc:
            raise UnknownRoleError(
                f"role {role!r} needs adapter {binding.adapter!r}, which is not registered"
            ) from exc

    def build_request(self, role: str, prompt: str, cwd: Path, **kwargs: object) -> AgentRequest:
        """Build a request for `role`, failing closed on an unhonourable restriction.

        `allowed_tools` is a request-level *request*; whether it reaches the
        provider command is entirely up to the bound adapter. An adapter that
        ignores it (codex builds its argv from a fixed sandbox mode) would
        drop the restriction silently, and neither a registry test asserting
        on the request nor an adapter test asserting on the argv can see it.
        So the join is checked here: a read-only role may only be bound to an
        adapter that declares `read_only_review`.
        """
        binding = self.binding_for(role)
        allowed = None
        if role in READ_ONLY_ROLES:
            adapter = self.adapter_for(role)
            if not adapter.supports("read_only_review"):
                raise ReadOnlyRoleUnsupportedError(
                    f"role {role!r} is read-only but is bound to adapter "
                    f"{binding.adapter!r}, which does not support read_only_review. "
                    "Bind the role to an adapter that does, or drop it from "
                    "READ_ONLY_ROLES -- dev-orch will not issue a request whose "
                    "restriction cannot be enforced."
                )
            allowed = list(READ_ONLY_TOOLS)
        return AgentRequest(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model_alias=binding.model,
            reasoning=binding.reasoning,
            allowed_tools=allowed,
            **kwargs,
        )
