"""Environment diagnosis. Every failure names its next action."""

import platform
import re
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
from dev_orchestration.domain.enums import Tier
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

    tracked_ignored = repo.tracked_ignored_paths()
    if tracked_ignored:
        preview = ", ".join(tracked_ignored[:3]) + ("..." if len(tracked_ignored) > 3 else "")
        checks.append(
            Check(
                "tracked files matched by gitignore",
                False,
                f"{len(tracked_ignored)} path(s): {preview}. Remove ignore rules or untrack these files.",
            )
        )
    else:
        checks.append(Check("tracked files matched by gitignore", True))

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
    checks.append(_agents_contract_check(repo.root / "AGENTS.md", config))
    for name, command in config.validation.items():
        checks.append(Check(f"validation:{name}", True, command.command))
    checks.extend(_proportionality_checks(config))
    return checks


def _section_lines(text: str, title: str) -> list[str]:
    headings = list(re.finditer(r"^## ([^\n]+)\s*$", text, flags=re.MULTILINE))
    matching = [match for match in headings if match.group(1) == title]
    if len(matching) != 1:
        reason = "missing" if not matching else "duplicated"
        raise ValueError(f"{title} section is {reason}")
    start = matching[0].end()
    following = next(
        (match.start() for match in headings if match.start() > matching[0].start()),
        len(text),
    )
    return text[start:following].splitlines()


def _code_bullets(lines: list[str], label: str) -> list[str]:
    markers = [index for index, line in enumerate(lines) if line.strip() == label]
    if len(markers) != 1:
        reason = "missing" if not markers else "duplicated"
        raise ValueError(f"{label.rstrip(':')} list marker is {reason}")
    values = []
    empty_list = False
    for line in lines[markers[0] + 1 :]:
        if not line.strip():
            if values:
                break
            continue
        if not line.startswith("- "):
            break
        if line == "- (none)":
            empty_list = True
            continue
        match = re.fullmatch(r"- `([^`]+)`", line)
        if not match:
            raise ValueError(f"unrecognized entry in {label.rstrip(':')} list")
        if match.group(1) != "(none)":
            values.append(match.group(1))
    if empty_list and values:
        raise ValueError(f"{label.rstrip(':')} list mixes entries with '(none)'")
    if not values and not empty_list:
        raise ValueError(f"{label.rstrip(':')} list is empty or unrecognized")
    return values


def _validation_commands(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    started = False
    for line in lines:
        if not line.strip() and not started:
            continue
        if not line.startswith("- "):
            if started:
                break
            raise ValueError("Validation command list is missing or unrecognized")
        started = True
        if line == "- (none — unproven yet — an unproven command is not a gate)":
            continue
        match = re.fullmatch(r"- `([^`]+)`: `(.*)`", line)
        if not match:
            raise ValueError("unrecognized entry in Validation command list")
        name, command = match.groups()
        if name in values:
            raise ValueError(f"Validation command {name!r} is duplicated")
        values[name] = command
    if not started:
        raise ValueError("Validation command list is missing or unrecognized")
    return values


def _agents_contract_check(path: Path, config: ProjectConfig) -> Check:
    """Compare only the repeated scope and validation settings in generated AGENTS.md."""
    if not path.is_file():
        return Check("AGENTS.md contract", False, "incomplete: AGENTS.md is missing")
    try:
        text = path.read_text(encoding="utf-8")
        scope = _section_lines(text, "Scope")
        validation = _section_lines(text, "Validation")
        agents_include = _code_bullets(scope, "Modifiable:")
        agents_exclude = _code_bullets(scope, "Never modifiable:")
        agents_validation = _validation_commands(validation)
    except (OSError, UnicodeError, ValueError) as exc:
        return Check("AGENTS.md contract", False, f"incomplete: {exc}")

    mismatches = []
    for field, agents_value, config_value in (
        ("scope.include", sorted(agents_include), sorted(config.scope.include)),
        ("scope.exclude", sorted(agents_exclude), sorted(config.scope.exclude)),
    ):
        if agents_value != config_value:
            mismatches.append(
                f"{field}: AGENTS.md={agents_value!r}; .ai/project.yaml={config_value!r}"
            )
    all_names = sorted(set(agents_validation) | set(config.validation))
    for name in all_names:
        agents_command = agents_validation.get(name, "<missing>")
        config_command = (
            config.validation[name].command if name in config.validation else "<missing>"
        )
        if agents_command != config_command:
            mismatches.append(
                f"validation:{name}: AGENTS.md={agents_command!r}; "
                f".ai/project.yaml={config_command!r}"
            )
    if mismatches:
        return Check(
            "AGENTS.md contract",
            False,
            "; ".join(mismatches) + ". Reconcile these values manually.",
        )
    return Check("AGENTS.md contract", True)


# Controls the runner actually reads. Anything else in a profile's `controls` map is
# carried into prompts as text but enforces nothing, so the config reads stricter than
# the system behaves. Surface that rather than letting it look like a live gate.
ENFORCED_PROFILE_CONTROLS = frozenset({"human_approval"})


def _proportionality_checks(config: ProjectConfig) -> list[Check]:
    checks: list[Check] = []

    trivial_validated = any(
        Tier.TRIVIAL in command.required_for for command in config.validation.values()
    )
    checks.append(
        Check(
            "validation covers trivial",
            trivial_validated,
            ""
            if trivial_validated
            else (
                "no validation command lists `trivial` in required_for, so trivial runs "
                "skip lint and tests and keep the slower implementation review instead; "
                "add `trivial` to required_for to make them cheap and checked"
            ),
        )
    )

    inert = sorted(
        {
            key
            for profile in config.profiles.definitions.values()
            for key in profile.controls
            if key not in ENFORCED_PROFILE_CONTROLS
        }
    )
    checks.append(
        Check(
            "profile controls enforced",
            not inert,
            ""
            if not inert
            else (
                f"declared but not enforced by the runner: {', '.join(inert)}; "
                "these reach agents as prompt text only — remove them or implement them"
            ),
        )
    )
    return checks
