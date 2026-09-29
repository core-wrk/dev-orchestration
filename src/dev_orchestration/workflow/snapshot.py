"""Exact, path-independent snapshot of a run's visible checkout changes."""

import hashlib
import json
import os
import stat

from dev_orchestration.git.repo import GitRepo


class SnapshotMismatch(RuntimeError):
    """The reserved checkout differs from the saved recovery point."""


def capture_snapshot(repo: GitRepo, base_commit: str) -> dict:
    entries: dict[str, dict] = {}
    for name in repo.change_inventory(base_commit):
        target = repo.root / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            entries[name] = {"kind": "deleted"}
            continue
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
            content = os.readlink(target).encode()
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
            content = target.read_bytes()
        else:
            raise SnapshotMismatch(f"cannot snapshot special changed path {name}")
        entries[name] = {
            "kind": kind,
            "mode": stat.S_IMODE(info.st_mode),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ignored = repo.ignored_paths()
    value = {
        "base": base_commit,
        "head": repo.current_commit(),
        "branch": repo.current_branch(),
        "porcelain": repo.run_git("status", "--porcelain=v1", "-z", strip=False),
        "entries": entries,
        "ignored": {
            name: {
                "kind": entry.kind,
                "mode": entry.mode,
                "sha256": entry.digest,
                "link_target": entry.link_target,
            }
            for name, entry in sorted(ignored.states.items())
        },
    }
    value["digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def require_snapshot(repo: GitRepo, expected: dict) -> None:
    actual = capture_snapshot(repo, expected["base"])
    if actual != expected:
        raise SnapshotMismatch(
            f"run worktree changed since checkpoint: expected {expected['digest']}, "
            f"found {actual['digest']}"
        )
