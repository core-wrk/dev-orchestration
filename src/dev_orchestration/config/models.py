"""Typed configuration. Invalid config must fail before any agent runs."""

import re

from pydantic import BaseModel, Field, field_validator

from dev_orchestration.domain.enums import ProjectClass, Tier

DENIED_COMMAND_TOKENS = frozenset(
    {"deploy", "wrangler", "publish", "push", "merge", "release"}
)


class DeniedCommandError(ValueError):
    """A configured command matched the non-overridable deny-list."""


def denied_tokens(command: str) -> set[str]:
    """Return the deny-list tokens present in `command`.

    Splits on every non-alphanumeric run so `deploy:prod` and `predeploy`
    are both reduced to comparable words. Fails closed by design: a false
    positive costs one renamed script, a false negative deploys production.
    """
    words = set(re.split(r"[^a-z0-9]+", command.lower()))
    return words & DENIED_COMMAND_TOKENS


class ValidationCommand(BaseModel):
    command: str
    required_for: list[Tier] = Field(default_factory=list)
    timeout_seconds: int = 1800

    @field_validator("command")
    @classmethod
    def reject_denied_commands(cls, value: str) -> str:
        found = denied_tokens(value)
        if found:
            raise DeniedCommandError(
                f"command {value!r} contains prohibited token(s) "
                f"{sorted(found)}; validation commands may not deploy, "
                f"publish, or write to a remote"
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


class GitPolicy(BaseModel):
    branch_prefix: str = "ai/"
    autonomous_local_commits: bool = True
    autonomous_push: bool = False


class ProjectConfig(BaseModel):
    schema_version: str = "1.0"
    project: ProjectMeta
    profiles: Profiles = Field(default_factory=Profiles)
    scope: Scope = Field(default_factory=Scope)
    validation: dict[str, ValidationCommand] = Field(default_factory=dict)
    git: GitPolicy = Field(default_factory=GitPolicy)


class RoleConfig(BaseModel):
    adapter: str
    model: str | None = None
    reasoning: str | None = None


class GlobalConfig(BaseModel):
    schema_version: str = "1.0"
    worktree_root: str = "~/.dev-orchestration/worktrees"
    codex_binary: str | None = None
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
