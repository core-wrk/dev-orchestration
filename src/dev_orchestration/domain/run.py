from datetime import datetime

from pydantic import BaseModel, Field

from dev_orchestration.domain.enums import ProjectClass, RunState, Tier


class GitBlock(BaseModel):
    base_commit: str | None = None
    branch: str | None = None
    worktree: str | None = None
    final_commit: str | None = None


class RoleBinding(BaseModel):
    adapter: str
    model_alias: str | None = None
    reasoning: str | None = None
    resolved_binary: str | None = None
    resolved_version: str | None = None


class RunManifest(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    repository: str
    workflow: str
    tier: Tier
    project_class: ProjectClass
    active_profiles: list[str] = Field(default_factory=list)
    plan_origin: str = "generated"
    roles: dict[str, RoleBinding] = Field(default_factory=dict)
    git: GitBlock = Field(default_factory=GitBlock)
    resolved_config: dict = Field(default_factory=dict)
    status: RunState = RunState.CREATED
    created_at: datetime | None = None
    completed_at: datetime | None = None
