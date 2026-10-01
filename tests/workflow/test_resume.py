"""Recovery continues the saved run instead of replaying completed stages."""

import json
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentInterruptedError,
    AgentResult,
    AgentTimeoutError,
)
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig, RoleConfig
from dev_orchestration.domain.enums import RunState
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.checkpoints import input_hashes
from dev_orchestration.workflow.cloud import link_cloud_session
from dev_orchestration.workflow.legacy import LegacyRecoveryError
from dev_orchestration.workflow.recovery import ResumeRefused, approve_run, resume_run
from dev_orchestration.workflow.runner import execute_run
from dev_orchestration.workflow.scheduler import SchedulerQueue

CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "demo", "class": "internal_utility"},
        "scope": {"include": ["src/"], "exclude": []},
        "context": {"persistent": []},
        "validation": {"unit": {"command": "true", "required_for": ["standard"]}},
    }
)
CRITERIA = ["the change exists"]
PASS = {"outcome": "PASS", "findings": []}
VERIFY = {
    "outcome": "PASS",
    "criteria": CRITERIA,
    "verdicts": [{"criterion": CRITERIA[0], "verdict": "PASS", "evidence": "diff"}],
    "unresolved_finding_ids": [],
}
ROLES = (
    "classifier",
    "planner",
    "plan_reviewer",
    "plan_reconciler",
    "implementation_worker",
    "implementation_reviewer",
    "verifier",
)


class Scripted:
    name = "fake"

    def __init__(self, *, timeout_worker=False):
        self.timeout_worker = timeout_worker
        self.seen = []

    def healthcheck(self):
        return AdapterStatus(name=self.name, available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.seen.append(request.role)
        if request.role == "implementation_worker":
            if self.timeout_worker:
                raise AgentTimeoutError(request.role, 7, self.name)
            (request.cwd / "src" / "app.py").write_text("x = 2\n")
        payload = {
            "classifier": {"tier": "standard", "rationale": "r", "profiles": []},
            "planner": "# Plan\n",
            "plan_reviewer": PASS,
            "implementation_worker": "done",
            "implementation_reviewer": PASS,
            "verifier": VERIFY,
        }[request.role]
        now = datetime.now(UTC)
        return AgentResult(self.name, None, 0, payload, now, now)


def registry(adapter):
    return RoleRegistry(
        {role: RoleConfig(adapter="fake", model="m") for role in ROLES},
        {"fake": adapter},
    )


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".ai/runs/\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(root)


def test_timeout_resumes_worker_without_repeating_plan(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_worker", "implementation_reviewer", "verifier"]
    assert json.loads((first.store_root / "manifest.json").read_text())["git"]["final_commit"]


def _continued_patch(tmp_path, content=None):
    patch = tmp_path / "continued.patch"
    patch.write_text(
        content
        or (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 3\n"
        )
    )
    return patch


def _pause_continuation_at_stage(tmp_path, pause_role="classifier"):
    class PauseClassifier(Scripted):
        def run(self, request):
            if request.role == pause_role:
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(PauseClassifier()),
        "continue saved change",
        tmp_path / "wt",
        CRITERIA,
        continue_patch=_continued_patch(tmp_path),
        retry_of="source-run",
    )
    return git, first, RunStore(git.root, first.run_id)


@pytest.mark.parametrize("pause_role", ["classifier", "plan_reviewer"])
def test_continued_patch_is_checkpointed_before_classifier_and_applied_on_resume(
    tmp_path, pause_role
):
    git, first, store = _pause_continuation_at_stage(tmp_path, pause_role)
    first_receipt = store.latest_checkpoint()
    assert "execution/continued-change.patch" in first_receipt["artifacts"]
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert "implementation_worker" not in adapter.seen
    assert Path(store.read_manifest().git.worktree, "src/app.py").read_text() == "x = 3\n"


def _pause_continuation_at_approval(tmp_path, continue_patch=None):
    class Substantial(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {"tier": "substantial", "rationale": "needs approval", "profiles": []},
                    now,
                    now,
                )
            return super().run(request)

    git = repo(tmp_path)
    config_data = CONFIG.model_dump(mode="json")
    config_data["approval"] = {"substantial": True}
    config_data["validation"]["unit"]["required_for"] = ["substantial"]
    config = ProjectConfig.model_validate(config_data)
    first = execute_run(
        git,
        config,
        registry(Substantial()),
        "continue saved change with approval",
        tmp_path / "wt",
        CRITERIA,
        continue_patch=continue_patch or _continued_patch(tmp_path),
        retry_of="source-run",
    )
    assert first.final_state is RunState.AWAITING_APPROVAL
    store = RunStore(git.root, first.run_id)
    assert "execution/continued-change.patch" in store.latest_checkpoint()["artifacts"]
    worktree = Path(store.read_manifest().git.worktree)
    link_cloud_session(
        git,
        first.run_id,
        "claude",
        "session-1",
        "environment-1",
        restored_worktree=worktree,
    )
    digest = approve_run(
        git, config, registry(Scripted()), first.run_id, restored_worktree=worktree
    )
    return git, config, first, store, worktree, digest


def test_continued_patch_survives_approval_gate_and_applies_after_approval(tmp_path):
    git, config, first, _store, worktree, digest = _pause_continuation_at_approval(tmp_path)
    adapter = Scripted()
    resumed = resume_run(
        git,
        config,
        registry(adapter),
        first.run_id,
        expected_checkpoint_digest=digest,
        restored_worktree=worktree,
    )
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert "implementation_worker" not in adapter.seen
    assert (worktree / "src/app.py").read_text() == "x = 3\n"


@pytest.mark.parametrize("tamper", ["delete", "edit"])
def test_tampered_continuation_after_approval_pause_refuses_before_provider(tmp_path, tamper):
    git, config, first, store, worktree, digest = _pause_continuation_at_approval(tmp_path)
    patch = store.root / "execution/continued-change.patch"
    if tamper == "delete":
        patch.unlink()
    else:
        patch.write_text("changed patch")
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="continued-change.patch"):
        resume_run(
            git,
            config,
            registry(adapter),
            first.run_id,
            expected_checkpoint_digest=digest,
            restored_worktree=worktree,
        )
    assert adapter.seen == []


def test_resume_escalates_when_continued_patch_no_longer_applies(tmp_path):
    bad = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1 +1 @@\n-not the current line\n+x = 3\n"
    )
    git, config, first, store, worktree, digest = _pause_continuation_at_approval(
        tmp_path, _continued_patch(tmp_path, bad)
    )
    source_patch = git.root / ".ai/runs/source-run/execution/final-change.patch"
    source_patch.parent.mkdir(parents=True)
    source_patch.write_text(bad)
    source_bytes = source_patch.read_bytes()
    adapter = Scripted()
    resumed = resume_run(
        git,
        config,
        registry(adapter),
        first.run_id,
        expected_checkpoint_digest=digest,
        restored_worktree=worktree,
    )
    assert resumed.final_state is RunState.ESCALATED
    assert "retry --fresh" in store.read_manifest().terminal_reason
    assert "implementation_worker" not in adapter.seen
    assert source_patch.read_bytes() == source_bytes


@pytest.mark.parametrize("pause_role", ["classifier", "plan_reviewer"])
@pytest.mark.parametrize("tamper", ["delete", "edit"])
def test_tampered_continuation_refuses_before_provider_or_worker(tmp_path, pause_role, tamper):
    git, first, store = _pause_continuation_at_stage(tmp_path, pause_role)
    patch = store.root / "execution/continued-change.patch"
    if tamper == "delete":
        patch.unlink()
    else:
        patch.write_text("changed patch")
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="continued-change.patch"):
        resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert adapter.seen == []


def test_unreceipted_continuation_inserted_into_fresh_retry_refuses(tmp_path):
    class PauseClassifier(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(PauseClassifier()),
        "fresh retry",
        tmp_path / "wt",
        CRITERIA,
        retry_of="source-run",
    )
    store = RunStore(git.root, first.run_id)
    store.write_text_artifact("execution/continued-change.patch", "spoofed")
    adapter = Scripted()
    with pytest.raises(
        ResumeRefused, match="unreceipted stage output execution/continued-change.patch"
    ):
        resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert adapter.seen == []


@pytest.mark.parametrize("edit_role", ["classifier", "plan_reviewer"])
def test_inflight_continuation_edit_blocks_next_checkpoint(tmp_path, monkeypatch, edit_role):
    from dev_orchestration.artifacts.store import CheckpointError

    original_checkpoint = RunStore.checkpoint
    rejected = []

    def observe_checkpoint(self, **kwargs):
        try:
            return original_checkpoint(self, **kwargs)
        except CheckpointError:
            rejected.append(True)
            raise

    monkeypatch.setattr(RunStore, "checkpoint", observe_checkpoint)
    patch = _continued_patch(tmp_path)

    class EditDuringStage(Scripted):
        def run(self, request):
            result = super().run(request)
            if request.role == edit_role:
                run_dir = max((git.root / ".ai/runs").iterdir(), key=lambda path: path.name)
                (run_dir / "execution/continued-change.patch").write_text(
                    "edited during provider call"
                )
            return result

    adapter = EditDuringStage()
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "continue saved change",
        tmp_path / "wt",
        CRITERIA,
        continue_patch=patch,
        retry_of="source-run",
    )
    assert rejected
    assert "implementation_worker" not in adapter.seen
    assert not any(
        json.loads(line).get("event") == "change_continued"
        for line in (outcome.store_root / "events.jsonl").read_text().splitlines()
    )


def test_old_manifest_keeps_hashes_and_resumes_with_cycle_validation_artifact(tmp_path):
    class PausedReviewer(Scripted):
        def run(self, request):
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(PausedReviewer()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, first.run_id)
    original_hashes = input_hashes(store, registry(Scripted()))
    raw = json.loads((store.root / "manifest.json").read_text())
    raw.pop("validation_repairs_reserved")
    raw.pop("validation_artifact")
    (store.root / "manifest.json").write_text(json.dumps(raw))
    assert input_hashes(store, registry(Scripted())) == original_hashes

    changed = CONFIG.model_dump(mode="json")
    changed["validation"]["unit"]["command"] = "false"
    with pytest.raises(ResumeRefused, match="changed|input"):
        resume_run(git, ProjectConfig.model_validate(changed), registry(Scripted()), first.run_id)

    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_reviewer", "verifier"]
    assert (store.root / "execution/validation-v1.json").is_file()


def test_pause_during_validation_repair_resumes_reserved_attempt(tmp_path):
    class PauseRepair(Scripted):
        worker_calls = 0

        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                self.worker_calls += 1
                (request.cwd / "src" / "app.py").write_text("x = 2\n")
                if self.worker_calls == 2:
                    raise AgentTimeoutError(request.role, 7, self.name)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "done", now, now)
            return super().run(request)

    class FinishRepair(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                (request.cwd / "src" / "app.py").write_text("x = 9\n")
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "done", now, now)
            return super().run(request)

    git = repo(tmp_path)
    config_data = CONFIG.model_dump(mode="json")
    config_data["validation"]["unit"]["command"] = "test \"$(cat src/app.py)\" = 'x = 9'"
    config = ProjectConfig.model_validate(config_data)
    first = execute_run(
        git,
        config,
        registry(PauseRepair()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, first.run_id)
    assert store.latest_checkpoint()["next_stage"] == "validation_repair"
    assert store.read_manifest().validation_repairs_reserved == 1

    adapter = FinishRepair()
    resumed = resume_run(git, config, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_worker", "implementation_reviewer", "verifier"]
    assert store.read_manifest().validation_repairs_reserved == 1
    assert (store.root / "execution/validation-repair-v1.json").is_file()


def test_signalled_worker_preserves_partial_diff_and_retries_same_stage(tmp_path):
    class SignalledWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "src" / "app.py").write_text("partial = True\n")
                raise AgentInterruptedError(request.role, self.name, 15)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(SignalledWorker()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    worktree = tmp_path / "wt" / "demo" / first.run_id
    assert (worktree / "src" / "app.py").read_text() == "partial = True\n"
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["implementation_worker", "implementation_reviewer", "verifier"]


def test_abrupt_worker_crash_requires_explicit_partial_work_adoption(tmp_path):
    class CrashingWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "src" / "app.py").write_text("partial = True\n")
                raise SystemExit(9)
            return super().run(request)

    git = repo(tmp_path)
    with pytest.raises(SystemExit):
        execute_run(
            git,
            CONFIG,
            registry(CrashingWorker()),
            "change app",
            tmp_path / "wt",
            CRITERIA,
        )
    run_id = next((git.root / ".ai/runs").iterdir()).name
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="--adopt-worktree"):
        resume_run(git, CONFIG, registry(adapter), run_id)
    assert adapter.seen == []
    resumed = resume_run(git, CONFIG, registry(adapter), run_id, adopt_worktree=True)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[0] == "implementation_worker"


@pytest.mark.parametrize(
    "checkpoint_stage,next_role",
    [
        ("implementation_worker", "implementation_worker"),
        ("validation", "implementation_reviewer"),
    ],
)
def test_crash_after_checkpoint_starts_next_unfinished_stage(
    tmp_path, monkeypatch, checkpoint_stage, next_role
):
    import dev_orchestration.workflow.runner as runner_module

    actual_record_stage = runner_module.record_stage

    def crash_after_checkpoint(*args, **kwargs):
        result = actual_record_stage(*args, **kwargs)
        if kwargs["next_stage"] == checkpoint_stage:
            raise SystemExit(9)
        return result

    git = repo(tmp_path)
    monkeypatch.setattr(runner_module, "record_stage", crash_after_checkpoint)
    with pytest.raises(SystemExit):
        execute_run(
            git,
            CONFIG,
            registry(Scripted()),
            "change app",
            tmp_path / "wt",
            CRITERIA,
        )
    run_id = next((git.root / ".ai/runs").iterdir()).name
    store = RunStore(git.root, run_id)
    approved_digest = store.read_manifest().plan_sha256
    monkeypatch.setattr(runner_module, "record_stage", actual_record_stage)
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[0] == next_role
    assert store.read_manifest().plan_sha256 == approved_digest


def test_interrupted_worker_cannot_hide_out_of_scope_ignored_write(tmp_path):
    class OutOfScopeTimeout(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "outside.log").write_text("hidden change\n")
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\noutside.log\n")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-q", "-m", "ignore log"], check=True)
    outcome = execute_run(
        git,
        CONFIG,
        registry(OutOfScopeTimeout()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.ESCALATED
    worktree = tmp_path / "wt" / "demo" / outcome.run_id
    assert not (worktree / "outside.log").exists()


def test_changed_partial_work_refuses_before_provider(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    worktree = tmp_path / "wt" / "demo" / first.run_id
    (worktree / "src" / "app.py").write_text("unauthorized change\n")
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="pause snapshot"):
        resume_run(git, CONFIG, registry(adapter), first.run_id, auto_resume=True)
    assert adapter.seen == []
    assert RunStore(git.root, first.run_id).read_manifest().auto_resume is False
    with pytest.raises(ResumeRefused, match="pause snapshot"):
        resume_run(git, CONFIG, registry(adapter), first.run_id, adopt_worktree=True)
    assert adapter.seen == []


def test_orphan_plan_output_cannot_supersede_receipted_plan(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_USAGE
    (first.store_root / "planning/plan-v4.md").write_text("orphan after crash\n")
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="unreceipted stage output planning/plan-v4.md"):
        resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert adapter.seen == []


@pytest.mark.parametrize("change", ["artifact", "configuration", "live_provider"])
def test_recovery_gates_refuse_before_provider_call(tmp_path, change):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_USAGE
    store = RunStore(git.root, first.run_id)
    config = CONFIG
    provider = None
    if change == "artifact":
        (first.store_root / "planning/plan-v3.md").write_text("changed\n")
    elif change == "configuration":
        changed = CONFIG.model_dump(mode="json")
        changed["validation"]["unit"]["command"] = "false"
        config = ProjectConfig.model_validate(changed)
    else:
        provider = subprocess.Popen(["sleep", "60"], start_new_session=True)
        store.record_active_provider("plan_reviewer", "fake", provider.pid)
    adapter = Scripted()
    try:
        with pytest.raises(ResumeRefused, match="."):
            resume_run(git, config, registry(adapter), first.run_id)
        assert adapter.seen == []
    finally:
        if provider is not None:
            os.killpg(provider.pid, signal.SIGTERM)
            provider.wait(timeout=5)


BLOCKING = {
    "outcome": "CHANGES_REQUIRED",
    "findings": [
        {
            "id": "x",
            "severity": "blocking",
            "summary": "missing check",
            "evidence_required": "test result",
        }
    ],
}


class QuotaAtThirdReview(Scripted):
    def __init__(self):
        super().__init__()
        self.reviews = 0
        self.reconciliations = 0

    def run(self, request):
        if request.role == "plan_reviewer":
            self.reviews += 1
            self.seen.append(request.role)
            now = datetime.now(UTC)
            if self.reviews == 3:
                return AgentResult(
                    self.name,
                    None,
                    0,
                    "provider error",
                    now,
                    now,
                    diagnostic={
                        "type": "error",
                        "error": "five-hour usage limit reached",
                        "reset_at": (now + timedelta(minutes=1)).isoformat(),
                    },
                )
            return AgentResult(self.name, None, 0, BLOCKING, now, now)
        if request.role == "plan_reconciler":
            self.reconciliations += 1
            self.seen.append(request.role)
            now = datetime.now(UTC)
            return AgentResult(
                self.name, None, 0, f"# Plan v{self.reconciliations + 1}\n", now, now
            )
        return super().run(request)


def test_third_review_quota_resumes_exact_plan_version(tmp_path):
    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    first_adapter = QuotaAtThirdReview()
    first = execute_run(
        git,
        CONFIG,
        registry(first_adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert first.final_state is RunState.PAUSED_USAGE
    assert first_adapter.reviews == 3
    assert (first.store_root / "planning/plan-v3.md").is_file()
    reviews_before = sorted((first.store_root / "review").glob("plan-review-v*.json"))
    assert len(reviews_before) == 2
    assert len(queue.due(datetime.now(UTC) + timedelta(minutes=2))) == 1

    adapter = Scripted()
    resumed = resume_run(
        git,
        CONFIG,
        registry(adapter),
        first.run_id,
        scheduler_queue=queue,
    )
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == [
        "plan_reviewer",
        "implementation_worker",
        "implementation_reviewer",
        "verifier",
    ]
    reviews_after = sorted((first.store_root / "review").glob("plan-review-v*.json"))
    assert len(reviews_after) == 3
    assert json.loads(reviews_after[0].read_text())["findings"][0]["id"] == "F001"
    assert json.loads(reviews_after[1].read_text())["findings"][0]["id"] == "F001"
    assert queue.due(datetime.now(UTC) + timedelta(minutes=2)) == []


def test_terminal_legacy_quota_creates_linked_run_without_changing_source(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    source = RunStore(git.root, first.run_id)
    source.update_manifest(
        status=RunState.FAILED,
        checkpoint=None,
        pause=None,
        terminal_reason="provider usage limit",
        schema_version="1.0",
    )
    for path in (source.root / "checkpoints").glob("*.json"):
        path.unlink()
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert resumed.run_id != first.run_id
    assert source.read_manifest().status is RunState.FAILED
    assert source.read_manifest().checkpoint is None
    assert RunStore(git.root, resumed.run_id).read_manifest().linked_source_run == first.run_id
    assert adapter.seen[0] == "plan_reviewer"


def test_legacy_dirty_worktree_requires_explicit_adoption(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    source = RunStore(git.root, first.run_id)
    source.update_manifest(checkpoint=None, pause=None, schema_version="1.0")
    for path in (source.root / "checkpoints").glob("*.json"):
        path.unlink()
    worktree = tmp_path / "wt" / "demo" / first.run_id
    (worktree / "src" / "app.py").write_text("x = 4\n")
    with pytest.raises(LegacyRecoveryError, match="--adopt-worktree.*Diff"):
        resume_run(git, CONFIG, registry(Scripted()), first.run_id)
    resumed = resume_run(git, CONFIG, registry(Scripted()), first.run_id, adopt_worktree=True)
    assert resumed.final_state is RunState.COMPLETE_LOCAL


def test_pause_before_classification_resumes_despite_persistent_context(tmp_path):
    class ClassifierTimeout(Scripted):
        def run(self, request):
            if request.role == "classifier" and not self.seen:
                self.seen.append("classifier")
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    (git.root / "AGENTS.md").write_text("rules\n")
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-q", "-m", "ctx"], check=True)
    config = CONFIG.model_copy(
        update={"context": CONFIG.context.model_copy(update={"persistent": ["AGENTS.md"]})}
    )
    first = execute_run(
        git, config, registry(ClassifierTimeout()), "change app", tmp_path / "wt", CRITERIA
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    resumed = resume_run(git, config, registry(Scripted()), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL


def test_pause_before_classification_refuses_changed_selected_spec_before_provider(tmp_path):
    class ClassifierTimeout(Scripted):
        def run(self, request):
            if request.role == "classifier" and not self.seen:
                self.seen.append("classifier")
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    spec_dir = git.root / "docs" / "specs"
    spec_dir.mkdir(parents=True)
    spec = spec_dir / "feature.md"
    spec.write_text("version one\n")
    subprocess.run(["git", "-C", str(git.root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "spec"], check=True)
    config = ProjectConfig.model_validate(
        CONFIG.model_dump(mode="json")
        | {"context": {"persistent": [], "on_demand": {"specifications": "docs/specs"}}}
    )
    first = execute_run(
        git,
        config,
        registry(ClassifierTimeout()),
        "Implement docs/specs/feature.md",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    spec.write_text("version two\n")
    resumed_adapter = Scripted()
    with pytest.raises(ResumeRefused, match="input changed"):
        resume_run(git, config, registry(resumed_adapter), first.run_id)
    assert resumed_adapter.seen == []


def test_pause_after_validation_repair_revalidates_without_rerunning_worker(tmp_path, monkeypatch):
    import dev_orchestration.workflow.runner as runner_module

    class RepairingWorker(Scripted):
        worker_calls = 0

        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                self.worker_calls += 1
                value = "x = 1\n" if self.worker_calls == 1 else "x = 9\n"
                (request.cwd / "src" / "app.py").write_text(value)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "done", now, now)
            return super().run(request)

    actual_record_stage = runner_module.record_stage

    def crash_after_repair_receipt(*args, **kwargs):
        result = actual_record_stage(*args, **kwargs)
        if (
            kwargs["next_stage"] == "validation"
            and "execution/validation-repair-v1.json" in kwargs["artifacts"]
        ):
            raise SystemExit(9)
        return result

    git = repo(tmp_path)
    config_data = CONFIG.model_dump(mode="json")
    config_data["validation"]["unit"]["command"] = "test \"$(cat src/app.py)\" = 'x = 9'"
    config = ProjectConfig.model_validate(config_data)
    first_adapter = RepairingWorker()
    monkeypatch.setattr(runner_module, "record_stage", crash_after_repair_receipt)
    with pytest.raises(SystemExit):
        execute_run(
            git,
            config,
            registry(first_adapter),
            "repair app",
            tmp_path / "wt",
            CRITERIA,
        )
    assert first_adapter.worker_calls == 2

    run_id = next((git.root / ".ai/runs").iterdir()).name
    store = RunStore(git.root, run_id)
    assert store.latest_checkpoint()["next_stage"] == "validation"
    assert "execution/validation-repair-v1.json" in store.latest_checkpoint()["artifacts"]
    monkeypatch.setattr(runner_module, "record_stage", actual_record_stage)

    resumed_adapter = RepairingWorker()
    resumed = resume_run(git, config, registry(resumed_adapter), run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert "implementation_worker" not in resumed_adapter.seen
    assert resumed_adapter.seen == ["implementation_reviewer", "verifier"]
    assert (store.root / "execution/validation-v2.json").is_file()
