"""Safe handoff from an external wakeup to the same provider cloud session."""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.run import CloudSession
from dev_orchestration.git.repo import GitCommandError, GitRepo
from dev_orchestration.workflow.recovery import ResumeRefused
from dev_orchestration.workflow.snapshot import require_snapshot


@dataclass(frozen=True)
class CloudInspection:
    accessible: bool
    idle: bool
    environment_persisted: bool
    reason: str = ""
    worktree_digest: str | None = None
    branch: str | None = None
    environment_id: str | None = None


@dataclass(frozen=True)
class CloudDelivery:
    accepted: bool
    session_id: str
    reason: str = ""
    permanent_failure: bool = False


class CloudGateway(Protocol):
    def inspect(self, session: CloudSession) -> CloudInspection: ...
    def deliver(self, session: CloudSession, message: str) -> object: ...
    def read_delivery(self, raw: object, session: CloudSession) -> CloudDelivery: ...


class ClaudeCloudGateway:
    """CLI follow-up exists, but automatic inspection awaits a verified live path."""

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def inspect(self, session: CloudSession) -> CloudInspection:
        return CloudInspection(
            False,
            False,
            False,
            "Claude cloud session state and unfinished checkout have not been verified live",
        )

    def deliver(self, session: CloudSession, message: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.binary, "-p", message, "--cloud", session.session_id, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def read_delivery(self, raw: object, session: CloudSession) -> CloudDelivery:
        if not isinstance(raw, subprocess.CompletedProcess):
            raise ResumeRefused("Claude cloud delivery returned an invalid result")
        try:
            result = json.loads(raw.stdout)
        except json.JSONDecodeError:
            result = {}
        accepted = (
            raw.returncode == 0
            and result.get("ok") is True
            and result.get("session_id") == session.session_id
        )
        reason = str(result.get("error") or raw.stderr or "delivery failed")
        permanent = any(
            phrase in reason.lower()
            for phrase in (
                "session not found",
                "archived",
                "not logged in",
                "authentication",
                "access denied",
                "organization's policy",
                "unsupported provider",
            )
        )
        return CloudDelivery(
            accepted,
            session.session_id,
            "" if accepted else reason,
            permanent_failure=not accepted and permanent,
        )


class CodexCloudGateway:
    def inspect(self, session: CloudSession) -> CloudInspection:
        return CloudInspection(False, False, False, "Codex Cloud auto-resume unavailable")

    def deliver(self, session: CloudSession, message: str) -> object:
        raise ResumeRefused("Codex Cloud auto-resume unavailable")

    def read_delivery(self, raw: object, session: CloudSession) -> CloudDelivery:
        return CloudDelivery(False, session.session_id, "Codex Cloud auto-resume unavailable")


def _repository_identity(repo: GitRepo) -> str:
    try:
        return repo.remote_url()
    except GitCommandError:
        return str(repo.root.resolve())


def link_cloud_session(
    repo: GitRepo,
    run_id: str,
    provider: str,
    session_id: str,
    environment_id: str,
    *,
    restored_worktree: Path | None = None,
) -> CloudSession:
    """Explicitly link only a fully intact, stopped dev-orch run."""
    if provider not in {"claude", "codex"} or not session_id or not environment_id:
        raise ResumeRefused("cloud provider, session ID, and environment ID are required")
    store = RunStore(repo.root, run_id)
    with store.claim():
        manifest = store.read_manifest()
        if manifest.status not in {
            RunState.PAUSED_USAGE,
            RunState.PAUSED_INTERRUPTED,
            RunState.AWAITING_APPROVAL,
        }:
            raise ResumeRefused(
                "only a paused run or one awaiting approval can link a cloud session"
            )
        try:
            receipt = store.verify_checkpoint()
        except CheckpointError as exc:
            raise ResumeRefused(str(exc)) from exc
        worktree = restored_worktree or Path(manifest.git.worktree or "")
        if not worktree.is_dir():
            raise ResumeRefused("saved worktree is missing")
        expected = receipt["worktree"]
        if manifest.pause is not None:
            if manifest.pause.snapshot_artifact is None:
                raise ResumeRefused("pause snapshot is missing")
            expected = json.loads(
                (store.root / manifest.pause.snapshot_artifact).read_text(encoding="utf-8")
            )
        require_snapshot(GitRepo(worktree), expected)
        linked = CloudSession(
            provider=provider,
            session_id=session_id,
            repository=_repository_identity(repo),
            branch=manifest.git.branch or "",
            environment_id=environment_id,
            last_state="linked",
        )
        store.update_manifest(cloud_session=linked)
        store.append_event(
            {"event": "cloud_session_linked", "provider": provider, "session_id": session_id}
        )
        return linked


def resume_cloud_due(
    repo: GitRepo,
    run_id: str,
    gateway: CloudGateway,
    *,
    now: datetime | None = None,
) -> CloudDelivery:
    """One claimed delivery; the cloud checkout subsequently calls normal resume."""
    store = RunStore(repo.root, run_id)
    with store.claim():
        manifest = store.read_manifest()
        session = manifest.cloud_session
        pause = manifest.pause
        now = now or datetime.now(UTC)
        if session is None or pause is None or manifest.status is not RunState.PAUSED_USAGE:
            raise ResumeRefused("run has no eligible cloud usage pause")
        if not manifest.auto_resume or pause.next_attempt_at is None or now < pause.next_attempt_at:
            raise ResumeRefused("cloud run is not due for automatic continuation")
        if session.last_delivery_digest is not None:
            raise ResumeRefused("cloud continuation is already queued")
        if session.last_state in {"archived", "missing"}:
            raise ResumeRefused(f"cloud session is {session.last_state}")
        if not session.continuation_verified:
            raise ResumeRefused("cloud continuation and worktree persistence need a live test")
        try:
            receipt = store.verify_checkpoint()
        except CheckpointError as exc:
            raise ResumeRefused(str(exc)) from exc
        digest = hashlib.sha256((store.root / manifest.checkpoint).read_bytes()).hexdigest()
        if session.branch != manifest.git.branch or session.repository != _repository_identity(
            repo
        ):
            raise ResumeRefused("cloud repository or branch identity changed")
        if not receipt["worktree"]:
            raise ResumeRefused("cloud checkpoint has no worktree snapshot")
        inspection = gateway.inspect(session)
        if not (inspection.accessible and inspection.idle and inspection.environment_persisted):
            raise ResumeRefused(inspection.reason or "cloud session is not safely resumable")
        expected_digest = pause.worktree_digest or receipt["worktree"]["digest"]
        if (
            inspection.worktree_digest != expected_digest
            or inspection.branch != session.branch
            or inspection.environment_id != session.environment_id
        ):
            raise ResumeRefused("cloud checkout snapshot, branch, or environment changed")
        message = (
            f"Continue dev-orch run {run_id} from checkpoint SHA-256 {digest}. "
            "From the repository containing its .ai/runs artifacts, locate the restored "
            "isolated run worktree and run "
            f"`dev-orch resume {run_id} --checkout <RUN_WORKTREE_PATH> "
            f"--checkpoint-digest {digest}`. Preserve the existing branch, partial changes, "
            "approved plan, scope, validation, and review gates. Do not push, open a PR, "
            "merge, deploy, or publish. If the run artifacts or worktree snapshot are missing, "
            "stop and report that state."
        )
        result = gateway.read_delivery(gateway.deliver(session, message), session)
        if result.session_id != session.session_id:
            raise ResumeRefused("cloud delivery changed the linked session ID")
        if not result.accepted:
            if result.permanent_failure:
                reason = result.reason.lower()
                state = (
                    "missing"
                    if "not found" in reason
                    else "archived"
                    if "archived" in reason
                    else "action_required"
                )
                store.update_manifest(
                    auto_resume=False,
                    cloud_session=session.model_copy(update={"last_state": state}),
                    pause=pause.model_copy(update={"next_attempt_at": None}),
                )
                store.append_event({"event": "cloud_auto_resume_stopped", "detail": result.reason})
                return result
            pause = pause.model_copy(
                update={
                    "next_attempt_at": now + timedelta(minutes=5),
                    "retry_count": pause.retry_count + 1,
                }
            )
            store.update_manifest(pause=pause)
            store.append_event({"event": "cloud_delivery_rejected", "detail": result.reason})
            return result
        session = session.model_copy(
            update={
                "last_state": "resume_queued",
                "last_delivery_digest": digest,
                "last_delivery_at": now,
            }
        )
        store.update_manifest(cloud_session=session)
        store.append_event({"event": "cloud_resume_queued", "checkpoint_digest": digest})
        return result
