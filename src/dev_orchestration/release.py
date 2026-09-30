"""Frozen releases: the dev-orch other repos and the scheduler run.

`promote` installs the latest commit as a non-editable copy under
~/.dev-orchestration/releases/<commit>/ and repoints ~/.local/bin/dev-orch at it.
Editing or switching branches in the source checkout never touches a release.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

RELEASES_DIR = Path.home() / ".dev-orchestration" / "releases"
BIN_DIR = Path.home() / ".local" / "bin"
KEEP_RELEASES = 3
COMMIT_FILE = "COMMIT"


class PromoteError(RuntimeError):
    """A release could not be built or activated; nothing was switched."""


def installed_commit() -> str:
    stamp = Path(sys.prefix).parent / COMMIT_FILE
    return stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else "dev (live checkout)"


def source_checkout() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        raise PromoteError("promote must run from the source checkout; use dev-orch-dev promote")
    return root


def promote(repo: Path, releases: Path = RELEASES_DIR, bin_dir: Path = BIN_DIR) -> str:
    commit = _run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"]).strip()
    release = releases / commit
    if not (release / "venv").is_dir():
        _build(repo, commit, releases)
    _activate(releases, release, bin_dir, repo)
    _prune(releases, release)
    return commit


def _build(repo: Path, commit: str, releases: Path) -> None:
    releases.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".build-{commit}-", dir=releases))
    try:
        source = work / "src"
        source.mkdir()
        archive = work / "src.tar"
        _run(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(archive), "HEAD"])
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        # Pin to the dependency versions the source checkout runs on today.
        constraints = work / "constraints.txt"
        constraints.write_text(
            _run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"]), encoding="utf-8"
        )
        venv = work / "venv"
        _run([sys.executable, "-m", "venv", str(venv)])
        _run([str(venv / "bin" / "pip"), "install", "-c", str(constraints), str(source)])
        _smoke_test(venv)
        shutil.rmtree(source)
        archive.unlink()
        constraints.unlink()
        (work / COMMIT_FILE).write_text(commit + "\n", encoding="utf-8")
        work.rename(releases / commit)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _smoke_test(venv: Path) -> None:
    _run([str(venv / "bin" / "dev-orch"), "--help"])
    _run(
        [
            str(venv / "bin" / "python"),
            "-I",
            "-c",
            "from dev_orchestration.roles.loader import ROLE_TEMPLATES, load_role_prompt;"
            "[load_role_prompt(r) for r in ROLE_TEMPLATES]",
        ],
        cwd=venv.parent,
    )


def _activate(releases: Path, release: Path, bin_dir: Path, repo: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    _replace_link(releases / "current", release)
    _replace_link(bin_dir / "dev-orch", releases / "current" / "venv" / "bin" / "dev-orch")
    live = repo / ".venv" / "bin" / "dev-orch"
    if live.exists():
        _replace_link(bin_dir / "dev-orch-dev", live)


def _replace_link(link: Path, target: Path) -> None:
    """Point `link` at `target` in one rename, so readers never see a missing link."""
    if link.exists() and not link.is_symlink():
        raise PromoteError(f"{link} is not a symlink; refusing to replace it")
    staging = link.with_name(link.name + ".new")
    staging.unlink(missing_ok=True)
    staging.symlink_to(target)
    os.replace(staging, link)


def _prune(releases: Path, current: Path) -> None:
    others = sorted(
        (
            p
            for p in releases.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p != current
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in others[KEEP_RELEASES - 1 :]:
        shutil.rmtree(old, ignore_errors=True)


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PromoteError(f"{' '.join(cmd[:4])} failed:\n{result.stderr.strip()}")
    return result.stdout
