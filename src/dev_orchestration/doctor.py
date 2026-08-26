"""Environment diagnosis. Every failure names its next action."""

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from dev_orchestration.adapters.base import AgentAdapter
from dev_orchestration.adapters.claude import ClaudeAdapter
from dev_orchestration.adapters.codex import CodexAdapter, discover_codex
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.git.repo import DirtyWorktreeError, GitCommandError, discover_repo
from dev_orchestration.scope import ScopeFence


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def render(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = "✓" if check.ok else "!"
        suffix = f" — {check.detail}" if check.detail else ""
        lines.append(f"{mark} {check.name}{suffix}")
    return "\n".join(lines)


def run_checks(
    cwd: Path,
    codex: AgentAdapter | None = None,
    claude: AgentAdapter | None = None,
) -> list[Check]:
    checks = [
        Check("macOS", platform.system() == "Darwin", platform.platform()),
        Check("python >= 3.11", sys.version_info >= (3, 11), platform.python_version()),
        Check("git available", shutil.which("git") is not None, _git_version()),
    ]

    codex = CodexAdapter(binary=discover_codex()) if codex is None else codex
    codex_status = codex.healthcheck()
    checks.append(Check("codex available", codex_status.available, codex_status.detail))
    if codex_status.available:
        caps = codex_status.capabilities
        checks.append(
            Check(
                "codex goal mode",
                caps.get("goal_headless", False),
                "goals feature enabled but no headless entry point; execution uses `codex exec`"
                if caps.get("goals_feature")
                else "no headless goal mode; execution uses `codex exec`",
            )
        )

    claude = ClaudeAdapter() if claude is None else claude
    claude_status = claude.healthcheck()
    checks.append(Check("claude available", claude_status.available, claude_status.detail))
    checks.extend(_repository_checks(cwd))
    return checks


def _git_version() -> str:
    if shutil.which("git") is None:
        return "install git"
    proc = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return proc.stdout.strip()


def _repository_checks(cwd: Path) -> list[Check]:
    try:
        repo = discover_repo(cwd)
    except GitCommandError:
        return [
            Check(
                "git repository",
                False,
                f"{cwd} is not inside a git repository; run `git init` or re-run from inside one.",
            )
        ]

    checks = [Check("git repository", True, str(repo.root))]

    try:
        repo.ensure_clean()
        checks.append(Check("repository clean", True))
    except DirtyWorktreeError:
        paths = [line[3:] for line in repo.status_porcelain().splitlines()]
        preview = ", ".join(paths[:3]) + ("..." if len(paths) > 3 else "")
        checks.append(
            Check(
                "repository clean",
                False,
                f"{len(paths)} uncommitted path(s): {preview}. "
                "Commit or stash before starting a run.",
            )
        )

    config_path = repo.root / ".ai" / "project.yaml"
    if not config_path.exists():
        checks.append(Check("project config", False, "no .ai/project.yaml; run `dev-orch init`"))
        return checks

    try:
        config = ProjectConfig.model_validate(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        checks.append(Check("project config", False, f"invalid .ai/project.yaml: {exc}"))
        return checks

    checks.append(Check("project config", True, f"class={config.project.project_class}"))
    fence = ScopeFence.from_config(config.scope)
    checks.append(
        Check(
            "scope fence",
            True,
            f"include={fence.include} exclude={fence.exclude}",
        )
    )
    agents_md_ok = (repo.root / "AGENTS.md").exists()
    checks.append(
        Check(
            "AGENTS.md",
            agents_md_ok,
            "" if agents_md_ok else "missing; run `dev-orch init`",
        )
    )
    for name, command in config.validation.items():
        checks.append(Check(f"validation:{name}", True, command.command))
    return checks
