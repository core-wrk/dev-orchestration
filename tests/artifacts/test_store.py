import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_orchestration.artifacts.store import (
    CheckpointError,
    PlanOverwriteError,
    RunStore,
    new_run_id,
    slugify,
)
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest


@pytest.fixture
def manifest():
    return RunManifest(
        run_id="20260825-013500_feature_session-reconciliation",
        repository="helmfast-OS",
        workflow="feature",
        tier=Tier.STANDARD,
        project_class=ProjectClass.INTERNAL_OPERATING_SYSTEM,
    )


@pytest.fixture
def store(tmp_path, manifest):
    s = RunStore(tmp_path, manifest.run_id)
    s.initialize(manifest)
    return s


def test_slugify_makes_a_filesystem_safe_token():
    assert slugify("Add Session Reconciliation!") == "add-session-reconciliation"


def test_run_id_follows_the_documented_format():
    when = datetime(2026, 8, 25, 1, 35, 0, tzinfo=UTC)
    assert new_run_id("feature", "session-reconciliation", now=when) == (
        "20260825-013500_feature_session-reconciliation"
    )


def test_initialize_creates_the_run_skeleton(store):
    for sub in ("planning", "execution", "review", "verification", "approval"):
        assert (store.root / sub).is_dir()
    assert (store.root / "manifest.json").is_file()


def test_plan_versions_are_append_only(store):
    first = store.write_plan_version("# Plan v1\n")
    second = store.write_plan_version("# Plan v2\n")
    assert first.name == "plan-v1.md"
    assert second.name == "plan-v2.md"
    assert first.read_text() == "# Plan v1\n"


def test_manifest_updates_persist(store):
    store.update_manifest(status=RunState.CLASSIFIED)
    assert store.read_manifest().status is RunState.CLASSIFIED


def test_events_append_one_json_object_per_line(store):
    store.append_event({"event": "STATE_CHANGED", "from": "CREATED", "to": "CLASSIFIED"})
    store.append_event({"event": "LOCAL_COMMIT_CREATED", "sha": "abc123"})
    lines = (store.root / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["sha"] == "abc123"
    assert "ts" in json.loads(lines[0])


def test_unreceipted_validation_repair_output_blocks_recovery(store):
    store.checkpoint(next_stage="validation", artifacts=[], worktree={}, inputs={})
    store.write_json_artifact("execution/validation-repair-v1.json", {"exit_code": 0})
    with pytest.raises(CheckpointError, match="validation-repair-v1.json"):
        store.verify_checkpoint()


def test_version_gap_finds_max_not_len(store):
    """Version numbering is max(existing)+1, not len(existing)+1.

    With plan-v1.md and plan-v5.md present, the next version should be v6,
    not v3 (which is what len(existing)+1 would give).
    """
    planning = store.root / "planning"
    (planning / "plan-v1.md").write_text("# Plan v1\n", encoding="utf-8")
    (planning / "plan-v5.md").write_text("# Plan v5\n", encoding="utf-8")
    result = store.write_plan_version("# Plan v6\n")
    assert result.name == "plan-v6.md"
    assert result.read_text() == "# Plan v6\n"


def test_sequential_plans_still_work(store):
    """Sequential writes v1, then v2, then v3."""
    first = store.write_plan_version("# Plan v1\n")
    second = store.write_plan_version("# Plan v2\n")
    third = store.write_plan_version("# Plan v3\n")
    assert first.name == "plan-v1.md"
    assert second.name == "plan-v2.md"
    assert third.name == "plan-v3.md"


def test_stray_files_do_not_crash_version_count(store):
    """A file named plan-vX.md (non-integer) is ignored in version counting."""
    planning = store.root / "planning"
    (planning / "plan-v1.md").write_text("# Plan v1\n", encoding="utf-8")
    (planning / "plan-vX.md").write_text("# Not a version\n", encoding="utf-8")
    result = store.write_plan_version("# Plan v2\n")
    assert result.name == "plan-v2.md"
    assert result.read_text() == "# Plan v2\n"


def test_a_concurrent_writer_cannot_silently_overwrite_a_plan(store, monkeypatch):
    """The losing side of the real race must not clobber the winner's plan.

    Reproduces the TOCTOU window rather than merely a stale directory listing:
    both writers glob and see nothing (empty glob), and the loser's existence
    check observes False (patched exists) because the winner has not created
    the file yet -- and then the winner creates it before the loser writes.

    An exists()-then-write implementation passes both observations and
    overwrites. Only an atomic exclusive create can fail here, so this test
    distinguishes the two implementations rather than just proving some guard
    exists.
    """
    first = store.write_plan_version("original plan\n")
    assert first.name == "plan-v1.md"

    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([]))
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(PlanOverwriteError):
        store.write_plan_version("clobbering plan\n")

    assert first.read_text() == "original plan\n"


def test_finding_index_round_trips_and_starts_empty(tmp_path):
    store = RunStore(tmp_path, "r")
    store.initialize(
        RunManifest(
            run_id="r",
            repository=str(tmp_path),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    assert store.finding_index() == {}
    store.record_finding_index({"src/app.py|missing bound check": "F001"})
    assert store.finding_index() == {"src/app.py|missing bound check": "F001"}
