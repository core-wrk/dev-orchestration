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
    timeout_seconds: int | None = None
    resolved_binary: str | None = None
    resolved_version: str | None = None


class PauseRecord(BaseModel):
    provider: str
    role: str
    limit_kind: str = "unknown"
    observed_at: datetime
    reset_at: datetime | None = None
    next_attempt_at: datetime | None = None
    retry_count: int = 0
    diagnostic: str = ""
    worktree_digest: str | None = None
    snapshot_artifact: str | None = None
    account_id: str | None = None
    raw_diagnostic_ref: str | None = None


class CloudSession(BaseModel):
    provider: str
    session_id: str
    repository: str
    branch: str
    environment_id: str
    last_state: str = "unknown"
    origin: str = "linked"
    continuation_verified: bool = False
    last_delivery_digest: str | None = None
    last_delivery_at: datetime | None = None


class RunManifest(BaseModel):
    schema_version: str = "1.1"
    run_id: str
    repository: str
    workflow: str
    tier: Tier
    project_class: ProjectClass
    available_profiles: list[str] = Field(default_factory=list)
    active_profiles: list[str] = Field(default_factory=list)
    plan_origin: str = "generated"
    plan_sha256: str | None = None
    approved_plan_version: str | None = None
    request_text: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    acceptance_criteria_source: str = "explicit"
    tier_override: Tier | None = None
    downgrade_reason: str | None = None
    approval_required: bool = False
    minimum_tier: Tier | None = None
    context_included: list[str] = Field(default_factory=list)
    context_excluded: list[str] = Field(default_factory=list)
    context_conflicts: list[str] = Field(default_factory=list)
    config_layers: list[dict] = Field(default_factory=list)
    roles: dict[str, RoleBinding] = Field(default_factory=dict)
    git: GitBlock = Field(default_factory=GitBlock)
    resolved_config: dict = Field(default_factory=dict)
    status: RunState = RunState.CREATED
    created_at: datetime | None = None
    completed_at: datetime | None = None
    terminal_reason: str | None = None
    terminal_cause: str | None = None
    retry_of: str | None = None
    checkpoint: str | None = None
    auto_resume: bool = False
    pause: PauseRecord | None = None
    cloud_session: CloudSession | None = None
    linked_source_run: str | None = None
    external_plan_artifact: str | None = None
    stage_attempts: dict[str, int] = Field(default_factory=dict)
