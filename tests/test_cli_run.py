import hashlib
import subprocess
from datetime import UTC, datetime

from typer.testing import CliRunner

import dev_orchestration.cli as cli_module
from dev_orchestration.adapters.base import AdapterStatus, AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.cli import app
from dev_orchestration.config.models import RoleConfig
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.reporting import describe_status, list_runs
from dev_orchestration.workflow.runner import execute_run as runner_execute_run

PLAN = "# external plan\n"
PASS = {"outcome": "PASS", "findings": []}
VERIFY = {
    "outcome": "PASS",
    "criteria": ["the flag exists"],
    "verdicts": [{"criterion": "the flag exists", "verdict": "PASS", "evidence": "seen"}],
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


class ScriptedAdapter:
    def __init__(self, tier="standard", profiles=None):
        self.seen = []
        self.tier = tier
        self.profiles = profiles or []

    def healthcheck(self):
        return AdapterStatus(name="fake", available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.seen.append(request.role)
        payload = {
            "classifier": {
                "tier": self.tier,
                "rationale": "test",
                "profiles": self.profiles,
            },
            "planner": PLAN,
            "plan_reviewer": PASS,
            "plan_reconciler": PLAN,
            "implementation_worker": "done",
            "implementation_reviewer": PASS,
            "verifier": VERIFY,
        }[request.role]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, payload, now, now)


def registry(adapter):
    return RoleRegistry(
        roles={role: RoleConfig(adapter="fake", model="m") for role in ROLES},
        adapters={"fake": adapter},
    )


def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(
        'schema_version: "1.0"\nproject:\n  name: demo\n  class: internal_utility\nscope:\n  include: ["src/"]\n  exclude: []\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def store(root, run_id, status):
    result = RunStore(root, run_id)
    result.initialize(
        RunManifest(
            run_id=run_id,
            repository=str(root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=status,
        )
    )
    return result


def test_status_shows_stage_and_next_action(tmp_path, monkeypatch):
    root = repo(tmp_path)
    store(root, "20260829-101500_standard_demo", RunState.IMPLEMENTATION_REVIEW)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "IMPLEMENTATION_REVIEW" in result.output and "Next:" in result.output


def test_status_never_prints_raw_json(tmp_path, monkeypatch):
    root = repo(tmp_path)
    store(root, "20260829-101500_standard_demo", RunState.VALIDATING)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status"])
    assert '{"' not in result.output and "schema_version" not in result.output


def test_every_run_state_has_next_action():
    for state in RunState:
        manifest = RunManifest(
            run_id="r",
            repository="x",
            workflow="w",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=state,
        )
        assert "Next:" in describe_status(manifest, [])


def test_runs_lists_newest_first(tmp_path):
    root = repo(tmp_path)
    store(root, "20260829-090000_standard_a", RunState.COMPLETE_LOCAL)
    store(root, "20260829-100000_standard_b", RunState.ESCALATED)
    assert [run_id for run_id, _ in list_runs(root)] == [
        "20260829-100000_standard_b",
        "20260829-090000_standard_a",
    ]


def test_status_unknown_run_fails_cleanly(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status", "no-such-run"])
    assert (
        result.exit_code == 1
        and "no-such-run" in result.output
        and "Traceback" not in result.output
    )


def test_run_refuses_unsupported_tier_without_traceback(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "rewrite auth", "--tier", "high_risk"])
    assert (
        result.exit_code == 1 and "high_risk" in result.output and "Traceback" not in result.output
    )


def test_run_refuses_dirty_repository_and_names_file(tmp_path, monkeypatch):
    root = repo(tmp_path)
    (root / "scratch.txt").write_text("dirty\n")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag"])
    assert (
        result.exit_code == 1
        and "scratch.txt" in result.output
        and "Traceback" not in result.output
    )


def _use_local_worktree(monkeypatch, tmp_path):
    def execute_in_tmp(repo, config, registry_value, request, _worktree_root, **kwargs):
        return runner_execute_run(
            repo,
            config,
            registry_value,
            request,
            tmp_path / "worktrees",
            **kwargs,
        )

    monkeypatch.setattr(cli_module, "execute_run", execute_in_tmp)


def _latest_manifest(root):
    manifests = sorted((root / ".ai" / "runs").glob("*/manifest.json"))
    return RunManifest.model_validate_json(manifests[-1].read_text(encoding="utf-8"))


def test_tier_override_is_effective_persisted_and_cannot_beat_minimum(tmp_path, monkeypatch):
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    repo(accepted_root)
    accepted_adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda config: registry(accepted_adapter))
    _use_local_worktree(monkeypatch, accepted_root)
    monkeypatch.chdir(accepted_root)
    accepted = CliRunner().invoke(app, ["run", "fix", "--tier", "trivial"])
    assert accepted.exit_code == 0, accepted.output
    accepted_manifest = _latest_manifest(accepted_root)
    assert accepted_manifest.tier is Tier.TRIVIAL
    assert accepted_manifest.tier_override is Tier.TRIVIAL

    minimum_root = tmp_path / "minimum"
    minimum_root.mkdir()
    repo(minimum_root)
    (minimum_root / ".ai" / "project.yaml").write_text(
        """schema_version: \"1.0\"
project:
  name: demo
  class: internal_utility
profiles:
  available: [auth]
  definitions:
    auth:
      minimum_tier: standard
      controls:
        security_review: required
scope:
  include: [\"src/\"]
  exclude: []
"""
    )
    subprocess.run(["git", "-C", str(minimum_root), "add", ".ai/project.yaml"], check=True)
    subprocess.run(["git", "-C", str(minimum_root), "commit", "-qm", "add profile"], check=True)
    minimum_adapter = ScriptedAdapter(tier="trivial", profiles=["auth"])
    monkeypatch.setattr(cli_module, "default_registry", lambda config: registry(minimum_adapter))
    _use_local_worktree(monkeypatch, minimum_root)
    monkeypatch.chdir(minimum_root)
    minimum = CliRunner().invoke(app, ["run", "fix", "--tier", "trivial"])
    assert minimum.exit_code == 0, minimum.output
    minimum_manifest = _latest_manifest(minimum_root)
    assert minimum_manifest.tier is Tier.STANDARD
    assert minimum_manifest.minimum_tier is Tier.STANDARD
    assert minimum_manifest.active_profiles == ["auth"]


def test_external_plan_is_imported_hashed_reviewed_and_not_regenerated(tmp_path, monkeypatch):
    root = tmp_path / "external"
    root.mkdir()
    repo(root)
    source = tmp_path / "plan.md"
    source.write_text(PLAN)
    adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda config: registry(adapter))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag", "--plan", str(source)])
    assert result.exit_code == 0, result.output
    manifest = _latest_manifest(root)
    assert manifest.plan_origin == "external"
    assert manifest.plan_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.approved_plan_version == "plan-v1.md"
    assert "planner" not in adapter.seen
    assert "plan_reviewer" in adapter.seen
    assert (
        root / ".ai" / "runs" / manifest.run_id / "planning" / "approved-plan.md"
    ).read_text() == PLAN
