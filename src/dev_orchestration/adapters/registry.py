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
    "verifier": RoleConfig(adapter="codex", model="sol", reasoning="high"),
}


class UnknownRoleError(KeyError):
    """A role was requested that has no configured binding."""


class RoleRegistry:
    def __init__(
        self, roles: dict[str, RoleConfig], adapters: dict[str, AgentAdapter]
    ) -> None:
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
        binding = self.binding_for(role)
        allowed = list(READ_ONLY_TOOLS) if role in READ_ONLY_ROLES else None
        return AgentRequest(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model_alias=binding.model,
            reasoning=binding.reasoning,
            allowed_tools=allowed,
            **kwargs,
        )
