"""Recovery continues the saved run instead of replaying completed stages."""

import json
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import dev_orchestration.workflow.validation as validation_module
from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentInterruptedError,
    AgentResult,
    AgentTimeoutError,
)
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.config.models import ApprovalPolicy, ProjectConfig, RoleConfig
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.checkpoints import input_hashes
from dev_orchestration.workflow.cloud import link_cloud_session
from dev_orchestration.workflow.legacy import LegacyRecoveryError
from dev_orchestration.workflow.recovery import ResumeRefused, approve_run, resume_run
from dev_orchestration.workflow.scheduler import SchedulerQueue
from dev_orchestration.workflow.validation import validation_file_hashes
from tests.workflow.run_helpers import execute_run

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
    (root / "src" / "app.py").chmod(0o644)
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


def test_resumed_worker_with_no_change_fails_before_validation(tmp_path):
    class PauseBeforeWriting(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class NoOpWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "I made no changes", now, now)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git, CONFIG, registry(PauseBeforeWriting()), "change app", tmp_path / "wt", CRITERIA
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    adapter = NoOpWorker()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)

    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert resumed.final_state is RunState.FAILED
    assert manifest.terminal_cause == "worker_no_changes"
    assert manifest.failure_class == "product"
    assert adapter.seen == ["implementation_worker"]
    assert (first.store_root / "execution/no-progress/implementation-worker.json").is_file()


def test_resumed_staged_edit_restored_to_base_fails_before_validation(tmp_path):
    class PauseBeforeWriting(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class StageThenRestore(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                app = request.cwd / "src" / "app.py"
                app.write_text("x = 2\n")
                subprocess.run(["git", "-C", str(request.cwd), "add", "src/app.py"], check=True)
                app.write_text("x = 1\n")
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "restored", now, now)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git, CONFIG, registry(PauseBeforeWriting()), "change app", tmp_path / "wt", CRITERIA
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    adapter = StageThenRestore()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)

    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert resumed.final_state is RunState.FAILED
    assert manifest.terminal_cause == "worker_no_changes"
    assert adapter.seen == ["implementation_worker"]
    assert not (first.store_root / "execution/validation-v1.json").exists()


def test_resumed_worker_keeps_saved_partial_change_when_it_adds_nothing(tmp_path):
    class PartialThenPause(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                (request.cwd / "src" / "app.py").write_text("x = 9\n")
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class NoOpWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "I made no changes", now, now)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git, CONFIG, registry(PartialThenPause()), "change app", tmp_path / "wt", CRITERIA
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    adapter = NoOpWorker()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[-2:] == ["implementation_reviewer", "verifier"]
    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert manifest.terminal_cause is None, "saved partial work must not be treated as no progress"
    assert manifest.git.final_commit is not None
    assert not (first.store_root / "execution/no-progress/implementation-worker.json").exists(), (
        "saved partial work must not record worker no progress"
    )


def _assert_resumed_remediation_no_progress(tmp_path, remediation_action):
    class PauseDuringRemediation(Scripted):
        worker_calls = 0

        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                self.worker_calls += 1
                if self.worker_calls == 1:
                    (request.cwd / "src" / "app.py").write_text("x = 2\n")
                    now = datetime.now(UTC)
                    return AgentResult(self.name, None, 0, "done", now, now)
                raise AgentTimeoutError(request.role, 7, self.name)
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                review = {
                    "outcome": "CHANGES_REQUIRED",
                    "findings": [
                        {
                            "id": "x",
                            "severity": "blocking",
                            "summary": "fix app",
                            "evidence_required": "tests",
                        }
                    ],
                }
                return AgentResult(self.name, None, 0, review, now, now)
            return super().run(request)

    class NoProgressRemediation(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                if remediation_action == "stage_identical":
                    subprocess.run(["git", "-C", str(request.cwd), "add", "src/app.py"], check=True)
                elif remediation_action == "commit_identical":
                    worktree_repo = GitRepo(request.cwd)
                    worktree_repo.run_git("commit", "--allow-empty", "-m", "no tree change")
                elif remediation_action == "cache":
                    cache = request.cwd / ".pytest_cache" / "v" / "cache"
                    cache.mkdir(parents=True)
                    (cache / "nodeids").write_text("cache")
                elif remediation_action == "chmod":
                    app = request.cwd / "src" / "app.py"
                    assert app.stat().st_mode & 0o777 == 0o644
                    app.chmod(0o600)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, "I made no changes", now, now)
            return super().run(request)

    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(PauseDuringRemediation()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    adapter = NoProgressRemediation()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)

    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert resumed.final_state is RunState.FAILED
    assert manifest.terminal_cause == "remediation_no_progress", remediation_action
    assert manifest.failure_class == "product"
    assert adapter.seen == ["implementation_worker"]
    assert (first.store_root / "execution/no-progress/remediation-v1.json").is_file()
    assert not (first.store_root / "execution/validation-v2.json").exists(), remediation_action


def test_resumed_staging_identical_content_is_no_progress(tmp_path):
    _assert_resumed_remediation_no_progress(tmp_path, "stage_identical")


def test_resumed_empty_commit_is_no_progress(tmp_path):
    _assert_resumed_remediation_no_progress(tmp_path, "commit_identical")


def test_resumed_cache_file_is_no_progress(tmp_path):
    _assert_resumed_remediation_no_progress(tmp_path, "cache")


def test_resumed_permission_change_is_no_progress(tmp_path):
    _assert_resumed_remediation_no_progress(tmp_path, "chmod")


def test_interrupted_planner_receives_scope_fence_before_and_after_resume(tmp_path):
    class InterruptPlannerOnce(Scripted):
        def __init__(self):
            super().__init__()
            self.planner_requests = []
            self.interrupt = True

        def run(self, request):
            if request.role == "planner":
                self.planner_requests.append(request)
                if self.interrupt:
                    self.interrupt = False
                    raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    scope = CONFIG.scope.model_copy(
        update={"include": ["src/", "docs/"], "exclude": ["docs/private/"]}
    )
    config = CONFIG.model_copy(update={"scope": scope})
    adapter = InterruptPlannerOnce()
    first = execute_run(
        git,
        config,
        registry(adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    resumed = resume_run(git, config, registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert len(adapter.planner_requests) == 2
    for request in adapter.planner_requests:
        scope_item = next(item for item in request.context.items if item.label == "scope_fence")
        assert '"include": ["src/", "docs/"]' in scope_item.content
        assert '"exclude": ["docs/private/"]' in scope_item.content


def test_seed_patch_receipt_worker_visibility_and_resume_once(tmp_path):
    class ObserveSeed(Scripted):
        def __init__(self, *, interrupt=False):
            super().__init__()
            self.interrupt = interrupt
            self.seed_contents = []

        def run(self, request):
            if request.role == "implementation_worker":
                self.seed_contents.append((request.cwd / "src/app.py").read_text())
                if self.interrupt:
                    raise AgentInterruptedError(request.role, self.name, 15)
            return super().run(request)

    git = repo(tmp_path)
    patch_path = tmp_path / "seed.patch"
    patch_path.write_bytes(
        b"diff --git a/src/app.py b/src/app.py\n"
        b"--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 99\n"
    )
    first_adapter = ObserveSeed(interrupt=True)
    first = execute_run(
        git,
        CONFIG,
        registry(first_adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    assert first_adapter.seed_contents == ["x = 99\n"]
    store = RunStore(git.root, first.run_id)
    manifest = store.read_manifest()
    assert manifest.seed_patch_artifact == "execution/seed-patch.patch"
    assert (
        manifest.seed_patch_sha256
        == __import__("hashlib").sha256(patch_path.read_bytes()).hexdigest()
    )
    assert (store.root / manifest.seed_patch_artifact).read_bytes() == patch_path.read_bytes()
    assert store.verify_checkpoint()["artifacts"]["execution/seed-patch-receipt.json"]

    resumed_adapter = ObserveSeed()
    resumed = resume_run(git, CONFIG, registry(resumed_adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert resumed_adapter.seed_contents == ["x = 99\n"]
    assert not (store.root / "execution/no-progress/implementation-worker.json").exists()
    applied = [
        line
        for line in (store.root / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "seed_patch_applied"
    ]
    assert len(applied) == 1
    saved_patch = store.root / manifest.seed_patch_artifact
    saved_patch.write_bytes(saved_patch.read_bytes() + b"tampered")
    with pytest.raises(CheckpointError, match="changed or is missing"):
        store.verify_checkpoint()


def test_seed_patch_binary_and_executable_changes_reach_worker(tmp_path):
    class ObserveFiles(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.binary = (request.cwd / "src/blob.bin").read_bytes()
                self.executable = (request.cwd / "src/tool").stat().st_mode & 0o111
            return super().run(request)

    git = repo(tmp_path)
    (git.root / "src/blob.bin").write_bytes(b"old\x00bytes")
    (git.root / "src/tool").write_text("#!/bin/sh\necho old\n")
    (git.root / "src/tool").chmod(0o644)
    subprocess.run(["git", "-C", str(git.root), "add", "src/blob.bin", "src/tool"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-q", "-m", "files"], check=True)
    (git.root / "src/blob.bin").write_bytes(b"new\x00bytes")
    (git.root / "src/tool").write_text("#!/bin/sh\necho new\n")
    (git.root / "src/tool").chmod(0o755)
    patch = subprocess.run(
        ["git", "-C", str(git.root), "diff", "--binary"], capture_output=True, check=True
    ).stdout
    subprocess.run(
        ["git", "-C", str(git.root), "checkout", "--", "src/blob.bin", "src/tool"], check=True
    )
    (git.root / "src/tool").chmod(0o644)
    patch_path = tmp_path / "binary-seed.patch"
    patch_path.write_bytes(patch)

    adapter = ObserveFiles()
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.binary == b"new\x00bytes"
    assert adapter.executable == 0o111


def test_out_of_scope_seed_patch_is_rolled_back_before_worker(tmp_path):
    git = repo(tmp_path)
    patch_path = tmp_path / "outside.patch"
    patch_path.write_bytes(
        b"diff --git a/README.md b/README.md\n"
        b"new file mode 100644\n--- /dev/null\n+++ b/README.md\n@@ -0,0 +1 @@\n+outside\n"
    )
    adapter = Scripted()
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "README.md" in outcome.reason
    assert "implementation_worker" not in adapter.seen
    worktree = tmp_path / "wt" / "repo" / outcome.run_id
    assert not worktree.exists()


def test_invalid_seed_patch_stops_before_worker(tmp_path):
    git = repo(tmp_path)
    patch_path = tmp_path / "invalid.patch"
    patch_path.write_bytes(b"not a git patch\n")
    adapter = Scripted()
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert outcome.final_state is RunState.FAILED
    assert "implementation_worker" not in adapter.seen
    assert not (tmp_path / "wt" / "repo" / outcome.run_id).exists()


def test_resumed_verification_can_complete_with_minor_finding_note(tmp_path):
    class PauseVerifier(Scripted):
        def run(self, request):
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {
                        "outcome": "PASS",
                        "findings": [
                            {"id": "F001", "severity": "minor", "summary": "small cleanup"}
                        ],
                    },
                    now,
                    now,
                )
            if request.role == "verifier":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class VerifyMinor(Scripted):
        def run(self, request):
            if request.role == "implementation_reviewer":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {
                        "outcome": "PASS",
                        "findings": [
                            {"id": "F001", "severity": "minor", "summary": "small cleanup"}
                        ],
                    },
                    now,
                    now,
                )
            if request.role == "verifier":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {
                        **VERIFY,
                        "outcome": "CHANGES_REQUIRED",
                        "unresolved_finding_ids": ["F001"],
                        "outcome_explanation": [
                            {"finding_id": "F001", "note": "cleanup is non-blocking"}
                        ],
                    },
                    now,
                    now,
                )
            return super().run(request)

    git = repo(tmp_path)
    paused = execute_run(
        git, CONFIG, registry(PauseVerifier()), "change app", tmp_path / "wt", CRITERIA
    )
    assert paused.final_state is RunState.PAUSED_INTERRUPTED
    resumed = resume_run(git, CONFIG, registry(VerifyMinor()), paused.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    result = json.loads((paused.store_root / "verification/final-verification.json").read_text())
    assert result["outcome_explanation"][0]["note"] == "cleanup is non-blocking"


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_resumed_fail_or_unknown_verification_cannot_complete(tmp_path, verdict):
    class PauseAtVerifier(Scripted):
        def run(self, request):
            if request.role == "verifier":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class ResumeWithVerdict(Scripted):
        def run(self, request):
            if request.role == "verifier":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {
                        **VERIFY,
                        "outcome": "CHANGES_REQUIRED",
                        "verdicts": [
                            {"criterion": CRITERIA[0], "verdict": verdict, "evidence": "checked"}
                        ],
                    },
                    now,
                    now,
                )
            return super().run(request)

    git = repo(tmp_path)
    paused = execute_run(
        git, CONFIG, registry(PauseAtVerifier()), "change app", tmp_path / "wt", CRITERIA
    )
    assert paused.final_state is RunState.PAUSED_INTERRUPTED

    resumed = resume_run(git, CONFIG, registry(ResumeWithVerdict()), paused.run_id)

    assert resumed.final_state is RunState.FAILED
    saved = json.loads((paused.store_root / "verification/final-verification.json").read_text())
    assert saved["verdicts"][0]["verdict"] == verdict


def test_resume_finishes_plan_receipt_after_plan_finalization_pause(tmp_path, monkeypatch):
    git = repo(tmp_path)
    approve_plan = RunStore.approve_plan

    def interrupt_before_approval(store, version):
        raise AgentTimeoutError("plan_reviewer", 7, "fake")

    monkeypatch.setattr(RunStore, "approve_plan", interrupt_before_approval)
    paused = execute_run(git, CONFIG, registry(Scripted()), "change app", tmp_path / "wt", CRITERIA)

    assert paused.final_state is RunState.PAUSED_INTERRUPTED
    paused_manifest = json.loads((paused.store_root / "manifest.json").read_text())
    checkpoint = json.loads((paused.store_root / paused_manifest["checkpoint"]).read_text())
    assert checkpoint["next_stage"] == "plan_finalization"
    assert not (paused.store_root / "planning/approved-plan.md").exists()
    assert not (paused.store_root / "planning/plan-review-receipt.json").exists()

    monkeypatch.setattr(RunStore, "approve_plan", approve_plan)
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), paused.run_id, now=datetime.now(UTC))

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    final_manifest = json.loads((resumed.store_root / "manifest.json").read_text())
    final_checkpoint = json.loads((resumed.store_root / final_manifest["checkpoint"]).read_text())
    assert "planning/plan-review-receipt.json" in final_checkpoint["artifacts"]
    assert not {"planner", "plan_reviewer", "plan_reconciler"} & set(adapter.seen)


def test_resume_refuses_changed_plan_review_receipt_before_provider_calls(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    receipt_path = first.store_root / "planning/plan-review-receipt.json"
    receipt_path.write_text(receipt_path.read_text() + " ")
    adapter = Scripted()

    with pytest.raises(ResumeRefused, match="changed or is missing"):
        resume_run(git, CONFIG, registry(adapter), first.run_id, now=datetime.now(UTC))

    assert adapter.seen == []


def test_resume_after_reviewed_plan_reuse_does_not_repeat_plan_review(tmp_path):
    git = repo(tmp_path)
    source_run = execute_run(
        git, CONFIG, registry(Scripted()), "change app", tmp_path / "source", CRITERIA
    )
    source = RunStore(git.root, source_run.run_id)
    paused = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "reused",
        CRITERIA,
        external_plan=source.root / "planning/approved-plan.md",
        reviewed_plan_from=source,
    )
    assert paused.final_state is RunState.PAUSED_INTERRUPTED

    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), paused.run_id, now=datetime.now(UTC))

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert not {"planner", "plan_reviewer", "plan_reconciler"} & set(adapter.seen)


def test_tampered_copied_plan_receipt_after_reuse_refuses_before_provider_calls(tmp_path):
    git = repo(tmp_path)
    source_run = execute_run(
        git, CONFIG, registry(Scripted()), "change app", tmp_path / "source", CRITERIA
    )
    source = RunStore(git.root, source_run.run_id)
    paused = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "reused",
        CRITERIA,
        external_plan=source.root / "planning/approved-plan.md",
        reviewed_plan_from=source,
    )
    receipt_path = paused.store_root / "planning/plan-review-receipt.json"
    receipt_path.write_text(receipt_path.read_text() + " ")
    adapter = Scripted()

    with pytest.raises(ResumeRefused, match="changed or is missing"):
        resume_run(git, CONFIG, registry(adapter), paused.run_id, now=datetime.now(UTC))

    assert adapter.seen == []


def test_tampered_prior_failure_snapshot_refuses_before_provider(tmp_path):
    git = repo(tmp_path)
    source = RunStore(git.root, "source-run")
    source.initialize(
        RunManifest(
            run_id="source-run",
            repository=str(git.root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=RunState.FAILED,
            terminal_cause="validation_failed",
            goal_id="tampered-prior-failure",
        )
    )
    source.append_event({"event": "run_cleanup_complete", "branch": None})

    class PausedWorker(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                self.seen.append(request.role)
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    first = execute_run(
        git,
        CONFIG,
        registry(PausedWorker()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        retry_of="source-run",
        retry_source=source,
    )
    feedback = first.store_root / "execution/prior-failures.json"
    assert feedback.is_file()
    feedback.write_text("{}", encoding="utf-8")
    adapter = Scripted()
    with pytest.raises(ResumeRefused, match="changed or is missing"):
        resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert adapter.seen == []


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
    assert not (store.root / "execution/no-progress/implementation-worker.json").exists()


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
    run_id = next(
        path.name
        for path in (git.root / ".ai/runs").iterdir()
        if path.is_dir() and path.name != ".goals"
    )
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
    run_id = next(
        path.name
        for path in (git.root / ".ai/runs").iterdir()
        if path.is_dir() and path.name != ".goals"
    )
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


def test_reliability_saved_inputs_classifier_pause_resumes_without_recorded_hashes(tmp_path):
    class ClassifierTimeout(Scripted):
        def run(self, request):
            if request.role == "classifier":
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nops/template.pptx\n")
    (git.root / "ops").mkdir()
    (git.root / "ops" / "template.pptx").write_bytes(b"synthetic template")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore fixture"], check=True)
    config = CONFIG.model_copy(update={"validation_files": ["ops/template.pptx"]})

    first = execute_run(
        git, config, registry(ClassifierTimeout()), "change app", tmp_path / "wt", CRITERIA
    )

    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, first.run_id)
    assert store.latest_checkpoint()["next_stage"] == "classifier"
    assert store.read_manifest().validation_input_hashes == {}

    resumed = resume_run(git, config, registry(Scripted()), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert store.read_manifest().validation_input_hashes == validation_file_hashes(
        git, config.validation_files
    )


@pytest.mark.parametrize("seeded", [False, True])
def test_trivial_run_resumes_before_and_after_contract_checkpoint(tmp_path, seeded):
    class ClassifierTimeout(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append("classifier")
                raise AgentTimeoutError("classifier", 7, self.name)
            return super().run(request)

    class TrivialWorker(Scripted):
        def __init__(self, *, expected_input, interrupt=False):
            super().__init__()
            self.expected_input = expected_input
            self.interrupt = interrupt

        def run(self, request):
            self.seen.append(request.role)
            if request.role == "classifier":
                payload = {"tier": "trivial", "rationale": "localized", "profiles": []}
            elif request.role == "implementation_worker":
                assert (request.cwd / "src/app.py").read_text() == self.expected_input
                if self.interrupt:
                    raise AgentInterruptedError(request.role, self.name, 15)
                (request.cwd / "src/app.py").write_text("x = 2\n")
                payload = "done"
            else:
                return super().run(request)
            now = datetime.now(UTC)
            return AgentResult(self.name, None, 0, payload, now, now)

    git = repo(tmp_path)
    patch_path = tmp_path / "seed.patch"
    patch_path.write_bytes(
        b"diff --git a/src/app.py b/src/app.py\n"
        b"--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 99\n"
    )
    expected_input = "x = 99\n" if seeded else "x = 1\n"
    first = execute_run(
        git,
        CONFIG,
        registry(ClassifierTimeout()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path if seeded else None,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED

    store = RunStore(git.root, first.run_id)
    assert store.latest_checkpoint()["next_stage"] == "classifier"
    assert store.read_manifest().plan_origin != "task_contract"
    assert not (store.root / "planning/task-contract.md").exists()
    paused_after_contract = resume_run(
        git,
        CONFIG,
        registry(TrivialWorker(expected_input=expected_input, interrupt=True)),
        first.run_id,
    )
    assert paused_after_contract.final_state is RunState.PAUSED_INTERRUPTED
    assert store.read_manifest().plan_origin == "task_contract"
    assert (store.root / "planning/task-contract.md").is_file()

    completed = resume_run(
        git,
        CONFIG,
        registry(TrivialWorker(expected_input=expected_input)),
        first.run_id,
    )
    assert completed.final_state is RunState.COMPLETE_LOCAL
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert sum(event.get("event") == "task_contract_written" for event in events) == 1
    assert sum(event.get("event") == "seed_patch_applied" for event in events) == int(seeded)


@pytest.mark.parametrize(
    "crash_point", ["after_write", "after_manifest", "after_manifest_changed_version"]
)
def test_trivial_contract_recovers_if_interrupted_before_its_checkpoint(
    tmp_path, monkeypatch, crash_point
):
    class Trivial(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                payload = {"tier": "trivial", "rationale": "localized", "profiles": []}
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, payload, now, now)
            return super().run(request)

    git = repo(tmp_path)
    original_write = RunStore.write_text_artifact
    original_update = RunStore.update_manifest

    def crash_after_contract_write(store, relative, content, *, immutable=False):
        result = original_write(store, relative, content, immutable=immutable)
        if relative == "planning/task-contract.md":
            raise SystemExit(9)
        return result

    def crash_after_contract_manifest_update(store, **updates):
        result = original_update(store, **updates)
        if updates.get("plan_origin") == "task_contract":
            raise SystemExit(9)
        return result

    if crash_point == "after_write":
        monkeypatch.setattr(RunStore, "write_text_artifact", crash_after_contract_write)
    else:
        monkeypatch.setattr(RunStore, "update_manifest", crash_after_contract_manifest_update)
    with pytest.raises(SystemExit):
        execute_run(git, CONFIG, registry(Trivial()), "change app", tmp_path / "wt", CRITERIA)

    run_root = git.root / ".ai" / "runs"
    run_id = next(path.name for path in run_root.iterdir() if (path / "manifest.json").is_file())
    store = RunStore(git.root, run_id)
    assert store.latest_checkpoint()["next_stage"] == "classifier"
    assert (store.read_manifest().plan_origin == "task_contract") == (crash_point != "after_write")
    assert (store.root / "planning/task-contract.md").is_file()

    monkeypatch.setattr(RunStore, "write_text_artifact", original_write)
    monkeypatch.setattr(RunStore, "update_manifest", original_update)
    if crash_point == "after_manifest_changed_version":
        store.update_manifest(approved_plan_version="different")
        with pytest.raises(ResumeRefused, match="saved request, policy, role binding"):
            resume_run(git, CONFIG, registry(Trivial()), run_id)
        return

    resumed = resume_run(git, CONFIG, registry(Trivial()), run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert sum(event.get("event") == "task_contract_written" for event in events) == 1


def test_trivial_contract_refuses_symlinked_planning_directory(tmp_path):
    class TimeoutClassifier(Scripted):
        def run(self, request):
            if request.role == "classifier":
                raise AgentTimeoutError(request.role, 7, self.name)
            return super().run(request)

    class TrivialClassifier(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                payload = {"tier": "trivial", "rationale": "localized", "profiles": []}
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 0, payload, now, now)
            return super().run(request)

    git = repo(tmp_path)
    paused = execute_run(
        git, CONFIG, registry(TimeoutClassifier()), "change app", tmp_path / "wt", CRITERIA
    )
    assert paused.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, paused.run_id)
    planning = store.root / "planning"
    planning.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    planning.symlink_to(outside, target_is_directory=True)

    resumed = resume_run(git, CONFIG, registry(TrivialClassifier()), paused.run_id)

    assert resumed.final_state is RunState.ESCALATED
    assert "symlink" in resumed.reason
    assert not (outside / "task-contract.md").exists()


def test_missing_validation_file_stops_before_planning_or_repair(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nops/template.pptx\n")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore fixture"], check=True)
    config = CONFIG.model_copy(update={"validation_files": ["ops/template.pptx"]})
    adapter = Scripted()
    outcome = execute_run(git, config, registry(adapter), "change app", tmp_path / "wt", CRITERIA)
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert outcome.final_state is RunState.FAILED
    assert manifest.failure_class == "environment"
    assert adapter.seen == ["classifier"]


def test_resume_rejects_changed_declared_validation_input(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nops/template.pptx\n")
    (git.root / "ops").mkdir()
    (git.root / "ops" / "template.pptx").write_bytes(b"before")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore fixture"], check=True)
    config = CONFIG.model_copy(update={"validation_files": ["ops/template.pptx"]})
    first = execute_run(
        git,
        config,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert manifest.validation_input_hashes["ops/template.pptx"]
    (git.root / "ops" / "template.pptx").write_bytes(b"after")
    with pytest.raises(ResumeRefused, match="declared validation files changed"):
        resume_run(git, config, registry(Scripted()), first.run_id)


def test_reliability_saved_inputs_resume_rejects_file_and_manifest_hash_tampering(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nops/template.pptx\n")
    (git.root / "ops").mkdir()
    (git.root / "ops" / "template.pptx").write_bytes(b"before")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore fixture"], check=True)
    config = CONFIG.model_copy(update={"validation_files": ["ops/template.pptx"]})
    first = execute_run(
        git,
        config,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, first.run_id)
    (git.root / "ops" / "template.pptx").write_bytes(b"after")
    store.update_manifest(
        validation_input_hashes=validation_file_hashes(git, config.validation_files)
    )

    with pytest.raises(ResumeRefused, match="saved request, policy, role binding, or context"):
        resume_run(git, config, registry(Scripted()), first.run_id)


def test_reliability_saved_inputs_resume_accepts_historical_receipt_without_hash_field(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nops/template.pptx\n")
    (git.root / "ops").mkdir()
    (git.root / "ops" / "template.pptx").write_bytes(b"stable input")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "ignore fixture"], check=True)
    config = CONFIG.model_copy(update={"validation_files": ["ops/template.pptx"]})
    first = execute_run(
        git,
        config,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    store = RunStore(git.root, first.run_id)
    receipt_path = store.root / store.read_manifest().checkpoint
    historical_receipt = json.loads(receipt_path.read_text())
    historical_receipt["inputs"].pop("validation_input_hashes")
    receipt_path.write_text(json.dumps(historical_receipt))

    resumed = resume_run(git, config, registry(Scripted()), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert "validation_input_hashes" not in json.loads(receipt_path.read_text())["inputs"]


def test_reliability_saved_inputs_resume_accepts_omitted_empty_default(tmp_path):
    git = repo(tmp_path)
    config_path = git.root / ".ai" / "project.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "project: {name: demo, class: internal_utility}\n"
        'scope: {include: ["src/"], exclude: []}\n'
        "context: {persistent: []}\n"
        'validation: {unit: {command: "true", required_for: [standard]}}\n'
        "validation_files: []\n"
    )
    subprocess.run(["git", "-C", str(git.root), "add", ".ai/project.yaml"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-qm", "add project config"], check=True)
    first = execute_run(
        git,
        CONFIG,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    config_path.write_text(config_path.read_text().replace("validation_files: []\n", ""))

    resumed = resume_run(git, CONFIG, registry(Scripted()), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL


def test_provider_execution_failure_is_recorded_as_environment_failure(tmp_path):
    class ProviderFailure(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(self.name, None, 2, "", now, now, stderr="provider failed")
            return super().run(request)

    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(ProviderFailure()), "change app", tmp_path / "wt", CRITERIA
    )
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert outcome.final_state is RunState.ESCALATED
    assert manifest.failure_class == "environment"
    assert manifest.terminal_cause == "environment"


def test_reliability_environment_shell_launch_failure_skips_validation_repair(
    tmp_path, monkeypatch
):
    git = repo(tmp_path)
    adapter = Scripted()
    original_run = subprocess.run

    def fail_validation_shell(command, **kwargs):
        if command[0] == "/bin/sh":
            raise FileNotFoundError("synthetic shell launch failure")
        return original_run(command, **kwargs)

    monkeypatch.setattr(validation_module.subprocess, "run", fail_validation_shell)
    outcome = execute_run(git, CONFIG, registry(adapter), "change app", tmp_path / "wt", CRITERIA)
    manifest = RunStore(git.root, outcome.run_id).read_manifest()

    assert outcome.final_state is RunState.FAILED
    assert manifest.failure_class == "environment"
    assert adapter.seen.count("implementation_worker") == 1


def test_reliability_environment_nonzero_validation_exit_remains_product_failure(tmp_path):
    git = repo(tmp_path)
    adapter = Scripted()
    validation = CONFIG.validation["unit"].model_copy(update={"command": "exit 3"})
    config = CONFIG.model_copy(update={"validation": {"unit": validation}})

    outcome = execute_run(git, config, registry(adapter), "change app", tmp_path / "wt", CRITERIA)
    manifest = RunStore(git.root, outcome.run_id).read_manifest()

    assert outcome.final_state is RunState.FAILED
    assert manifest.failure_class == "product"
    assert adapter.seen.count("implementation_worker") == 2


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
                if self.worker_calls == 1:
                    (request.cwd / "src" / "initial-attempt.txt").write_text("attempted\n")
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

    run_id = next(
        path.name
        for path in (git.root / ".ai/runs").iterdir()
        if path.is_dir() and path.name != ".goals"
    )
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


def test_resumed_run_missing_required_dependency_fails_before_verifier(tmp_path, monkeypatch):
    import dev_orchestration.workflow.runner as runner_module

    git = repo(tmp_path)
    config_data = CONFIG.model_dump(mode="json")
    config_data["validation_links"] = [".venv", "node_modules"]
    config_data["required_validation_links"] = ["node_modules"]
    config = ProjectConfig.model_validate(config_data)
    actual_record_stage = runner_module.record_stage

    def crash_after_validation_checkpoint(*args, **kwargs):
        result = actual_record_stage(*args, **kwargs)
        if kwargs.get("next_stage") == "validation":
            raise SystemExit(9)
        return result

    first_adapter = Scripted()
    monkeypatch.setattr(runner_module, "record_stage", crash_after_validation_checkpoint)
    with pytest.raises(SystemExit):
        execute_run(
            git,
            config,
            registry(first_adapter),
            "change app",
            tmp_path / "wt",
            CRITERIA,
        )
    run_dirs = [
        path
        for path in (git.root / ".ai/runs").iterdir()
        if path.is_dir() and path.name != ".goals"
    ]
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    monkeypatch.setattr(runner_module, "record_stage", actual_record_stage)

    resumed_adapter = Scripted()
    resumed = resume_run(git, config, registry(resumed_adapter), run_id)
    manifest = RunManifest.model_validate_json(
        (resumed.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert resumed.final_state is RunState.FAILED
    assert manifest.terminal_cause == "environment"
    assert "verifier" not in resumed_adapter.seen


def test_ignored_out_of_scope_seed_patch_is_rolled_back_before_worker(tmp_path):
    git = repo(tmp_path)
    patch_path = tmp_path / "ignored.patch"
    patch_path.write_bytes(
        b"diff --git a/.ai/runs/escape.txt b/.ai/runs/escape.txt\n"
        b"new file mode 100644\n--- /dev/null\n+++ b/.ai/runs/escape.txt\n"
        b"@@ -0,0 +1 @@\n+outside\n"
    )
    adapter = Scripted()
    outcome = execute_run(
        git,
        CONFIG,
        registry(adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert ".ai/runs/escape.txt" in outcome.reason
    assert "implementation_worker" not in adapter.seen
    assert not (tmp_path / "wt" / "repo" / outcome.run_id).exists()


@pytest.mark.parametrize(
    ("patch_bytes", "expected_state", "reason_fragment"),
    [
        (
            (
                b"diff --git a/.ai/runs/escape.txt b/.ai/runs/escape.txt\n"
                b"new file mode 100644\n--- /dev/null\n+++ b/.ai/runs/escape.txt\n"
                b"@@ -0,0 +1 @@\n+outside\n"
            ),
            RunState.ESCALATED,
            ".ai/runs/escape.txt",
        ),
        (b"not a git patch\n", RunState.FAILED, "saved seed patch could not be applied"),
    ],
)
def test_bad_seed_patch_on_approved_resume_is_terminal(
    tmp_path, patch_bytes, expected_state, reason_fragment
):
    class HighRisk(Scripted):
        def run(self, request):
            if request.role == "classifier":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    {"tier": "substantial", "rationale": "approval-gate fixture", "profiles": []},
                    now,
                    now,
                )
            return super().run(request)

    git = repo(tmp_path)
    config = CONFIG.model_copy(update={"approval": ApprovalPolicy(substantial=True)})
    patch_path = tmp_path / "bad-seed.patch"
    patch_path.write_bytes(patch_bytes)
    first_adapter = HighRisk()
    first = execute_run(
        git,
        config,
        registry(first_adapter),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        seed_patch=patch_path,
    )
    assert first.final_state is RunState.AWAITING_APPROVAL
    assert "implementation_worker" not in first_adapter.seen

    store = RunStore(git.root, first.run_id)
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
        git, config, registry(first_adapter), first.run_id, restored_worktree=worktree
    )

    resumed_adapter = Scripted()
    resumed = resume_run(
        git,
        config,
        registry(resumed_adapter),
        first.run_id,
        expected_checkpoint_digest=digest,
        restored_worktree=worktree,
    )

    assert resumed.final_state is expected_state
    assert reason_fragment in resumed.reason
    assert "implementation_worker" not in resumed_adapter.seen
