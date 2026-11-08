from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from dev_orchestration.domain.enums import ProjectClass, RunState, Tier

MAX_REMEDIATION_CYCLES = 2


class GitBlock(BaseModel):
    base_commit: str | None = None
    branch: str | None = None
    worktree: str | None = None
    final_commit: str | None = None


class RoleBinding(BaseModel):
    adapter: str
    model_alias: str | None = None
    model_id: str | None = None
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


class StageActor(BaseModel):
    role: str
    provider: str | None = None
    model: str | None = None


class RepairBudget(BaseModel):
    used: int
    allowed: int
    exhausted: bool


class StopRecord(StageActor):
    stage: RunState | Literal["PLANNING", "PLAN_REVIEW", "PLAN_RECONCILIATION"]
    run_state: RunState | None = None
    category: Literal[
        "reviewer rejected",
        "validation failed",
        "plan rejected",
        "model behaviour",
        "infrastructure",
        "operator cancelled",
        "unknown",
        "context or token limit",
    ]
    evidence: str
    repairs: dict[str, RepairBudget]


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
    failure_class: (
        Literal["product", "evidence", "environment", "framework", "human_decision"] | None
    ) = None
    failure_reason: str | None = None
    failure_signature: str | None = None
    retry_of: str | None = None
    goal_id: str | None = None
    epic_id: str | None = None
    goal_attempt: int | None = None
    goal_generation: int = 0
    checkpoint: str | None = None
    auto_resume: bool = False
    pause: PauseRecord | None = None
    cloud_session: CloudSession | None = None
    linked_source_run: str | None = None
    restart_mode: Literal["check_only", "remediate"] | None = None
    restart_from: Literal["validation", "review", "verification", "remediation"] | None = None
    restart_reason: str | None = None
    restart_code_sha256: str | None = None
    restart_source_base_commit: str | None = None
    restart_destination_base_commit: str | None = None
    external_plan_artifact: str | None = None
    seed_patch_artifact: str | None = None
    seed_patch_sha256: str | None = None
    plan_review_provenance: dict = Field(default_factory=dict)
    stage_attempts: dict[str, int] = Field(default_factory=dict)
    validation_repairs_reserved: int = 0
    stage_actor: StageActor | None = None
    stage_error: Literal["provider_context_limit"] | None = None
    repair_rounds: dict[str, int] = Field(default_factory=dict)
    stop: StopRecord | None = None
    validation_artifact: str | None = None
    validation_input_hashes: dict[str, str] = Field(default_factory=dict)
