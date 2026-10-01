"""Run artifacts under .ai/runs/<run-id>/.

Durable decision artifacts live here and are committed with the code they
explain. Plan versions are immutable once written.
"""

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.artifacts.events import append_event
from dev_orchestration.domain.run import RunManifest

SUBDIRECTORIES = ("planning", "execution", "review", "verification", "approval", "checkpoints")


class PlanOverwriteError(RuntimeError):
    """An attempt was made to rewrite a reviewed plan version."""


class NoApprovedPlanError(RuntimeError):
    """Execution was attempted before an immutable plan was approved."""


class RunClaimError(RuntimeError):
    """Another process owns the run."""


class CheckpointError(RuntimeError):
    """Recovery evidence is missing or changed."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def new_run_id(workflow: str, slug: str, now: datetime | None = None) -> str:
    # Run IDs use UTC timestamps for DST-safe lexical and monotonic ordering.
    # Naive local timestamps break sort order during DST fall-back (same hour repeats).
    # Note: if RunManifest.created_at is added later, it must also use UTC to prevent
    # silent disagreement between folder name and manifest.
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{workflow}_{slug}"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        target = self.root / "manifest.json"
        temporary = self.root / f".manifest-{os.getpid()}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(manifest.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _sync_directory(self.root)

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )

    def update_manifest(self, **fields: object) -> RunManifest:
        manifest = self.read_manifest().model_copy(update=fields)
        self._write_manifest(manifest)
        return manifest

    @contextmanager
    def claim(self, *, wait_seconds: float = 0):
        """Hold one cross-process run claim across all recovery decisions and work."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "claim.lock").open("a+b") as handle:
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise RunClaimError(f"run {self.run_id} is already active") from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def checkpoint(
        self,
        *,
        next_stage: str,
        artifacts: list[str],
        worktree: dict,
        inputs: dict[str, str],
        cycle: int = 0,
        attempt: int = 1,
    ) -> dict:
        """Publish immutable evidence before advancing the manifest pointer."""
        previous = self.latest_checkpoint()
        sequence = previous["sequence"] + 1 if previous else 1
        all_artifacts = set(previous["artifacts"]) if previous else set()
        all_artifacts.update(artifacts)
        hashes = {}
        for name in sorted(all_artifacts):
            target = self.root / name
            try:
                with target.open("rb") as handle:
                    content = handle.read()
                    if (
                        previous
                        and name in previous["artifacts"]
                        and name not in artifacts
                        and hashlib.sha256(content).hexdigest() != previous["artifacts"][name]
                    ):
                        raise CheckpointError(f"checkpoint artifact {name} changed or is missing")
                    hashes[name] = hashlib.sha256(content).hexdigest()
                    os.fsync(handle.fileno())
            except FileNotFoundError as exc:
                raise CheckpointError(f"checkpoint artifact {name} changed or is missing") from exc
            _sync_directory(target.parent)
        receipt = {
            "sequence": sequence,
            "next_stage": next_stage,
            "cycle": cycle,
            "attempt": attempt,
            "artifacts": hashes,
            "worktree": worktree,
            "inputs": inputs,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        name = f"checkpoints/{sequence:04d}.json"
        self.write_json_artifact(name, receipt, immutable=True)
        self.update_manifest(checkpoint=name)
        return receipt

    def latest_checkpoint(self) -> dict | None:
        name = self.read_manifest().checkpoint
        if name is None:
            return None
        if not re.fullmatch(r"checkpoints/\d{4,}\.json", name):
            raise CheckpointError("invalid checkpoint path in manifest")
        path = self.root / name
        if not path.is_file():
            raise CheckpointError(f"checkpoint {name} is missing")
        return json.loads(path.read_text(encoding="utf-8"))

    def verify_checkpoint(self) -> dict:
        receipt = self.latest_checkpoint()
        if receipt is None:
            raise CheckpointError("run has no recovery checkpoint")
        for name, digest in receipt["artifacts"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise CheckpointError(f"invalid checkpoint artifact {name}")
            target = self.root / name
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise CheckpointError(f"checkpoint artifact {name} changed or is missing")
        # An output created just before a crash has no completed-stage receipt.
        # Refuse it explicitly so directory-based version lookup cannot select it.
        completed = set(receipt["artifacts"])
        output_patterns = (
            "planning/plan-v*.md",
            "planning/approved-plan.md",
            "planning/task-contract.md",
            "planning/plan-review-receipt.json",
            "review/plan-review-v*.json",
            "review/implementation-review-v*.json",
            "execution/worker-result.json",
            "execution/validation-v*.json",
            "execution/validation-repair-v*.json",
            "execution/remediation-v*.json",
            "execution/continued-change.patch",
            "verification/final-verification.json",
        )
        for pattern in output_patterns:
            for path in self.root.glob(pattern):
                name = path.relative_to(self.root).as_posix()
                if name not in completed:
                    raise CheckpointError(
                        f"unreceipted stage output {name}; inspect the interrupted attempt"
                    )
        return receipt

    def write_plan_version(self, content: str | bytes) -> Path:
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
            if isinstance(content, bytes):
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
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
            with target.open("xb") as handle:
                handle.write(version.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
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
        copied = self.write_plan_version(content)
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
            if immutable:
                handle.flush()
                os.fsync(handle.fileno())
        if immutable:
            _sync_directory(target.parent)
        return target

    def write_bytes_artifact(
        self, relative: str, content: bytes, *, immutable: bool = False
    ) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "xb" if immutable else "wb"
        with target.open(mode) as handle:
            handle.write(content)
            if immutable:
                handle.flush()
                os.fsync(handle.fileno())
        if immutable:
            _sync_directory(target.parent)
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
            "process_birth": _process_birth(pid),
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
        birth = value.get("process_birth")
        if birth is None:
            raise CheckpointError("provider process start time is missing; cannot terminate safely")
        if birth != _process_birth(pid):
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

    def active_provider_alive(self) -> bool:
        if not self.active_provider_path.is_file():
            return False
        value = json.loads(self.active_provider_path.read_text(encoding="utf-8"))
        pid = value.get("pid")
        if not isinstance(pid, int):
            raise CheckpointError("active provider record has no valid PID")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.clear_active_provider()
            return False
        except PermissionError:
            return True
        birth = value.get("process_birth")
        if birth is not None and birth != _process_birth(pid):
            self.clear_active_provider()
            return False
        return True


def _process_birth(pid: int) -> str | None:
    """OS process start string, used with PID to detect recycled process IDs."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None
