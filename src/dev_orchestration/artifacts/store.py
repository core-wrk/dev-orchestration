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
        next_version = len(existing) + 1
        target = planning / f"plan-v{next_version}.md"
        if target.exists():
            raise PlanOverwriteError(f"{target} already exists; plans are immutable")
        target.write_text(content, encoding="utf-8")
        return target

    def append_event(self, event: dict) -> None:
        append_event(self.root / "events.jsonl", event)
