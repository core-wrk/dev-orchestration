"""Logical roles resolve to a provider adapter plus a model alias here.

Nowhere else in the codebase should a model name appear.
"""

from pathlib import Path

from dev_orchestration.adapters.base import AgentAdapter, AgentRequest
from dev_orchestration.adapters.claude import READ_ONLY_TOOLS, ClaudeAdapter
from dev_orchestration.adapters.codex import CodexAdapter, discover_codex
from dev_orchestration.config.models import GlobalConfig, ProjectConfig, RoleConfig

# Only the implementation worker may edit the worktree. Both adapters must
# enforce this boundary for every other role, regardless of its prompt.
READ_ONLY_ROLES = frozenset(
    {
        "classifier",
        "planner",
        "plan_reviewer",
        "plan_reconciler",
        "implementation_reviewer",
        "verifier",
    }
)

DEFAULT_ROLES: dict[str, RoleConfig] = {
    # The 600s timeout is a second, cheap line of defense on top of the
    # allowedTools restriction: classification in particular should never
    # need anywhere near that long.
    "classifier": RoleConfig(
        adapter="codex", model="luna", reasoning="high", timeout_seconds=600
    ),
    "planner": RoleConfig(adapter="claude", model="claude-opus-5-5", reasoning="medium"),
    "plan_reviewer": RoleConfig(adapter="codex", model="sol", reasoning="high"),
    "plan_reconciler": RoleConfig(adapter="claude", model="claude-sonnet-5-5", reasoning="high"),
    "implementation_worker": RoleConfig(
        adapter="codex", model="luna", reasoning="high", timeout_seconds=1800
    ),
    "implementation_reviewer": RoleConfig(
        adapter="claude", model="claude-opus-5-5", reasoning="high"
    ),
    "verifier": RoleConfig(adapter="codex", model="luna", reasoning="high"),
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
        kwargs.setdefault("timeout_seconds", binding.timeout_seconds)
        return AgentRequest(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model_alias=binding.model,
            reasoning=binding.reasoning,
            allowed_tools=allowed,
            **kwargs,
        )


def default_registry(
    project_config: ProjectConfig | None = None,
    global_config: GlobalConfig | None = None,
) -> RoleRegistry:
    """Bind roles with framework < global < project precedence."""
    global_config = global_config or GlobalConfig()
    roles = dict(DEFAULT_ROLES)
    roles.update(global_config.roles)
    if project_config is not None:
        roles.update(project_config.roles)
    codex_override = None
    if global_config.codex_binary:
        codex_override = Path(global_config.codex_binary).expanduser()
    adapters: dict[str, AgentAdapter] = {
        "codex": CodexAdapter(binary=discover_codex(codex_override)),
        "claude": ClaudeAdapter(),
    }
    return RoleRegistry(roles=roles, adapters=adapters)
