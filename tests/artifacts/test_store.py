import json
from datetime import UTC, datetime

import pytest

from dev_orchestration.artifacts.store import (
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
