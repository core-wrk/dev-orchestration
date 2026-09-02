"""Typed configuration. Invalid config must fail before any agent runs."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from dev_orchestration.domain.enums import ProjectClass, Tier

DENIED_COMMAND_TOKENS = frozenset({"deploy", "wrangler", "publish", "push", "merge", "release"})


class DeniedCommandError(ValueError):
    """A configured command matched the non-overridable deny-list."""


def denied_tokens(command: str) -> set[str]:
    """Return the deny-list tokens present in `command`.

    Searches for each deny-list token as a substring in the lowercased command.
    This catches commands like `npm run predeploy`, `npm run redeploy`, etc.
    Fails closed by design: a false positive costs one renamed script, a false
    negative deploys production silently. The deny-list is not overridable.
    """
    lowered = command.lower()
    found = set()
    for token in DENIED_COMMAND_TOKENS:
        if token in lowered:
            found.add(token)
    return found


class ValidationCommand(BaseModel):
    command: str
    required_for: list[Tier] = Field(default_factory=list)
    timeout_seconds: int = 1800

    model_config = {"frozen": True}

    @field_validator("command")
    @classmethod
    def reject_denied_commands(cls, value: str) -> str:
        found = denied_tokens(value)
        if found:
            raise DeniedCommandError(
                f"command {value!r} contains prohibited token(s) "
                f"{sorted(found)}; the deny-list is not overridable. "
                f"validation commands may not deploy, publish, or write to a remote"
            )
        return value


class Scope(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=list)


class ProjectMeta(BaseModel):
    name: str
    project_class: ProjectClass = Field(alias="class")

    model_config = {"populate_by_name": True}


class Profiles(BaseModel):
    available: list[str] = Field(default_factory=list)
    definitions: dict[str, "ProfileConfig"] = Field(default_factory=dict)


class ProfileConfig(BaseModel):
    minimum_tier: Tier | None = None
    controls: dict[str, object] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)


class GitPolicy(BaseModel):
    branch_prefix: str = "ai/"
    autonomous_local_commits: bool = True
    autonomous_push: bool = False


class ContextPolicy(BaseModel):
    persistent: list[str] = Field(default_factory=lambda: ["AGENTS.md", ".ai/context.md"])
    on_demand: dict[str, str] = Field(default_factory=dict)
    include_prior_artifacts: Literal["relevant_only", "none"] = "relevant_only"
    conflict_policy: Literal["fail_closed", "escalate"] = "fail_closed"
    persistent_budget_bytes: int = 8192


class ApprovalPolicy(BaseModel):
    """Human approval gates for tiers that may change consequential systems."""

    substantial: bool = False
    high_risk: bool = True


class ProjectConfig(BaseModel):
    schema_version: str = "1.0"
    project: ProjectMeta
    profiles: Profiles = Field(default_factory=Profiles)
    scope: Scope = Field(default_factory=Scope)
    validation: dict[str, ValidationCommand] = Field(default_factory=dict)
    git: GitPolicy = Field(default_factory=GitPolicy)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    roles: dict[str, "RoleConfig"] = Field(default_factory=dict)
    protected: dict[str, object] = Field(default_factory=dict)


class RoleConfig(BaseModel):
    adapter: str
    model: str | None = None
    reasoning: str | None = None


class GlobalConfig(BaseModel):
    schema_version: str = "1.0"
    worktree_root: str = "~/.dev-orchestration/worktrees"
    codex_binary: str | None = None
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
