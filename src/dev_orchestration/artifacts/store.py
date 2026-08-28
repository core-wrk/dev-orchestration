"""Run artifacts under .ai/runs/<run-id>/.

Durable decision artifacts live here and are committed with the code they
explain. Plan versions are immutable once written.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.artifacts.events import append_event
from dev_orchestration.domain.run import RunManifest

SUBDIRECTORIES = ("planning", "execution", "review", "verification", "approval")


class PlanOverwriteError(RuntimeError):
    """An attempt was made to rewrite a reviewed plan version."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def new_run_id(workflow: str, slug: str, now: datetime | None = None) -> str:
    # Run IDs use UTC timestamps for DST-safe lexical and monotonic ordering.
    # Naive local timestamps break sort order during DST fall-back (same hour repeats).
    # Note: if RunManifest.created_at is added later, it must also use UTC to prevent
    # silent disagreement between folder name and manifest.
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{workflow}_{slug}"


class RunStore:
    def __init__(self, repo_root: Path, run_id: str) -> None:
        self.repo_root = repo_root
        self.run_id = run_id
        self.root = repo_root / ".ai" / "runs" / run_id

    def initialize(self, manifest: RunManifest) -> None:
        for sub in SUBDIRECTORIES:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._write_manifest(manifest)

    def _write_manifest(self, manifest: RunManifest) -> None:
        (self.root / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )

    def update_manifest(self, **fields: object) -> RunManifest:
        manifest = self.read_manifest().model_copy(update=fields)
        self._write_manifest(manifest)
        return manifest

    def write_plan_version(self, content: str) -> Path:
        planning = self.root / "planning"
        existing = sorted(planning.glob("plan-v*.md"))

        # Parse version numbers from filenames and find the maximum.
        # Stray files like plan-vX.md (non-integer) are ignored.
        max_version = 0
        for path in existing:
            match = re.match(r"plan-v(\d+)\.md$", path.name)
            if match:
                version = int(match.group(1))
                max_version = max(max_version, version)

        next_version = max_version + 1
        target = planning / f"plan-v{next_version}.md"
        # Exclusive create, not exists-then-write. With max+1 numbering the
        # target cannot already exist single-threaded, so a separate exists()
        # check was unreachable -- and as the concurrent-write guard it was
        # kept for, it did not work: two writers both glob, both compute the
        # same next version, both see nothing, and one silently overwrites the
        # other's plan. "x" makes the check and the create one atomic
        # operation in the filesystem, which is the only place it can be.
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise PlanOverwriteError(f"{target} already exists; plans are immutable") from exc
        return target

    def append_event(self, event: dict) -> None:
        append_event(self.root / "events.jsonl", event)
