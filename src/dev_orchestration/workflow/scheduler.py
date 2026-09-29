"""Local due-time index; run manifests make every continuation decision."""

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_QUEUE = Path.home() / ".local" / "share" / "dev-orch" / "scheduler" / "queue.json"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.dev-orch.scheduler.plist"
LAUNCH_LABEL = "com.dev-orch.scheduler"


class SchedulerError(RuntimeError):
    """Local scheduler configuration is unavailable."""


class SchedulerQueue:
    def __init__(self, path: Path = DEFAULT_QUEUE) -> None:
        self.path = path

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, entries: dict) -> None:
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _key(repo: Path, run_id: str) -> str:
        return f"{repo.resolve()}\0{run_id}"

    def register(self, repo: Path, run_id: str, due: datetime) -> None:
        with self._locked():
            entries = self._read()
            entries[self._key(repo, run_id)] = {
                "repository": str(repo.resolve()),
                "run_id": run_id,
                "due": due.astimezone(UTC).isoformat(),
            }
            self._write(entries)

    def cancel(self, repo: Path, run_id: str) -> None:
        if not self.path.is_file():
            return
        with self._locked():
            entries = self._read()
            entries.pop(self._key(repo, run_id), None)
            self._write(entries)

    def due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        with self._locked():
            return [
                entry
                for entry in self._read().values()
                if datetime.fromisoformat(entry["due"]) <= now
            ]


def enable_launchd(plist: Path = LAUNCH_AGENT) -> Path:
    binary = shutil.which("dev-orch")
    if binary is None:
        raise SchedulerError("dev-orch is not on PATH; install it before enabling the scheduler")
    plist.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [binary, "scheduler", "tick"],
        "StartInterval": 60,
        "RunAtLoad": True,
    }
    temporary = plist.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(value))
    os.replace(temporary, plist)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and "already" not in proc.stderr.lower():
        plist.unlink()
        raise SchedulerError(f"launchd refused scheduler: {proc.stderr.strip()}")
    return plist


def disable_launchd(plist: Path = LAUNCH_AGENT) -> None:
    if not plist.exists():
        return
    proc = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SchedulerError(f"launchd could not stop scheduler: {proc.stderr.strip()}")
    plist.unlink()
