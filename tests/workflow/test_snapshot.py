"""Saved checkout evidence includes files Git omits from status."""

import subprocess

import pytest

from dev_orchestration.workflow.snapshot import SnapshotMismatch, capture_snapshot, require_snapshot
from tests.workflow.test_resume import repo


def test_ignored_file_content_is_part_of_exact_resume_snapshot(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\nignored.log\n")
    subprocess.run(["git", "-C", str(git.root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git.root), "commit", "-q", "-m", "ignore log"], check=True)
    base = git.current_commit()
    ignored = git.root / "ignored.log"
    ignored.write_text("first\n")
    snapshot = capture_snapshot(git, base)
    assert snapshot["ignored"]["ignored.log"]["sha256"]

    ignored.write_text("second\n")
    with pytest.raises(SnapshotMismatch, match="changed since checkpoint"):
        require_snapshot(git, snapshot)
