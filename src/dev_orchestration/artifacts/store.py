"""Run artifacts under .ai/runs/<run-id>/.

Durable decision artifacts live here and are committed with the code they
explain. Plan versions are immutable once written.
"""

import hashlib
import json
import os
import re
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.artifacts.events import append_event
from dev_orchestration.domain.run import RunManifest

SUBDIRECTORIES = ("planning", "execution", "review", "verification", "approval")


class PlanOverwriteError(RuntimeError):
    """An attempt was made to rewrite a reviewed plan version."""


class NoApprovedPlanError(RuntimeError):
    """Execution was attempted before an immutable plan was approved."""


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

    def plan_versions(self) -> list[Path]:
        versions = []
        for path in (self.root / "planning").glob("plan-v*.md"):
            match = re.fullmatch(r"plan-v(\d+)\.md", path.name)
            if match:
                versions.append((int(match.group(1)), path))
        return [path for _, path in sorted(versions)]

    def approve_plan(self, version_path: Path) -> Path:
        """Atomically create the one approved contract; never replace it."""
        version = version_path.resolve()
        planning = (self.root / "planning").resolve()
        try:
            version.relative_to(planning)
        except ValueError as exc:
            raise PlanOverwriteError(f"plan {version_path} is outside this run") from exc
        if version not in {path.resolve() for path in self.plan_versions()}:
            raise PlanOverwriteError(f"plan {version_path} is not a stored plan version")
        target = self.root / "planning" / "approved-plan.md"
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(version.read_text(encoding="utf-8"))
        except FileExistsError as exc:
            raise PlanOverwriteError(
                f"{target} already exists; an approved contract is immutable within a run"
            ) from exc
        digest = hashlib.sha256(version.read_bytes()).hexdigest()
        self.update_manifest(approved_plan_version=version.name, plan_sha256=digest)
        self.append_event({"event": "plan_approved", "version": version.name, "sha256": digest})
        return target

    def approved_plan(self) -> str:
        target = self.root / "planning" / "approved-plan.md"
        if not target.is_file():
            raise NoApprovedPlanError(f"{target} does not exist; no plan has been approved")
        return target.read_text(encoding="utf-8")

    def import_external_plan(self, source: Path) -> tuple[Path, str]:
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        copied = self.write_plan_version(content.decode("utf-8"))
        self.update_manifest(plan_origin="external", plan_sha256=digest)
        self.append_event(
            {
                "event": "plan_imported",
                "source": str(source.resolve()),
                "sha256": digest,
                "stored_as": copied.name,
            }
        )
        return copied, digest

    def write_text_artifact(self, relative: str, content: str, *, immutable: bool = False) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "x" if immutable else "w"
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return target

    def write_json_artifact(self, relative: str, value: object, *, immutable: bool = False) -> Path:
        return self.write_text_artifact(
            relative, json.dumps(value, indent=2, default=str) + "\n", immutable=immutable
        )

    def write_versioned_json(self, prefix: str, value: object) -> Path:
        directory = self.root / "review"
        existing = []
        for path in directory.glob(f"{prefix}-v*.json"):
            match = re.fullmatch(rf"{re.escape(prefix)}-v(\d+)\.json", path.name)
            if match:
                existing.append(int(match.group(1)))
        version = max(existing, default=0) + 1
        return self.write_json_artifact(f"review/{prefix}-v{version}.json", value, immutable=True)

    def finding_index(self) -> dict[str, str]:
        """Map each finding identity key to the ID assigned in this run."""
        path = self.root / "review" / "finding-index.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def record_finding_index(self, index: dict[str, str]) -> None:
        self.write_json_artifact("review/finding-index.json", index)

    def append_event(self, event: dict) -> None:
        append_event(self.root / "events.jsonl", event)

    @property
    def active_provider_path(self) -> Path:
        return self.root / "execution" / "active-provider.json"

    def record_active_provider(self, role: str, provider: str, pid: int) -> None:
        """Record a provider PID so a separate `cancel` command can stop it."""
        value = {
            "role": role,
            "provider": provider,
            "pid": pid,
            "process_group": pid,
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.write_json_artifact("execution/active-provider.json", value)
        self.append_event({"event": "provider_started", **value})

    def clear_active_provider(self) -> None:
        if self.active_provider_path.exists():
            self.active_provider_path.unlink()

    def stop_active_provider(self) -> bool:
        """Best-effort termination of the recorded provider process group."""
        if not self.active_provider_path.is_file():
            return False
        value = json.loads(self.active_provider_path.read_text(encoding="utf-8"))
        pid = value.get("pid")
        pgid = value.get("process_group")
        if not isinstance(pid, int) or not isinstance(pgid, int):
            self.clear_active_provider()
            return False
        try:
            if os.getpgid(pid) != pgid:
                self.clear_active_provider()
                return False
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                os.killpg(pgid, signal.SIGKILL)
            self.append_event({"event": "provider_cancelled", "pid": pid, "process_group": pgid})
            return True
        except ProcessLookupError:
            return False
        finally:
            self.clear_active_provider()
