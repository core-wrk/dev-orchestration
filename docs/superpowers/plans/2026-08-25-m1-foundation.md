# dev-orchestration M1 (Foundation and Onboarding) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `dev-orch` CLI foundation — typed configuration, Git/worktree isolation, the scope fence, the run-artifact store, both provider adapters, `init`, and `doctor` — then onboard three helmfast repositories to the repository contract.

**Architecture:** A Python package exposing a Typer CLI. Configuration resolves through six layers into a frozen `ResolvedConfig`. Every provider is reached through an `AgentAdapter` protocol, so workflow code never sees CLI syntax. Git operations shell out to `git` with argument arrays; push, merge, and deploy have no implementation. M1 builds no workflow — it builds the substrate M2's loop runs on.

**Tech Stack:** Python ≥3.11, Typer, Pydantic v2, PyYAML, pytest, ruff. Providers are the installed `codex` and `claude` CLIs, invoked as subprocesses.

**Spec:** `docs/superpowers/specs/2026-08-25-dev-orchestration-v1-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Platform:** macOS. **Python:** `requires-python = ">=3.11"`.
- **Package:** `dev_orchestration` under `src/`. **CLI executable:** `dev-orch`.
- **Every subprocess call uses an argument array.** Never `shell=True`, never an f-string interpolated into a command. This applies to `git` and to both providers.
- **No push, merge, or deploy code path exists.** Not a flag defaulting off — no implementation. `git/guards.py` holds functions that exist only to raise.
- **The string `--dangerously-bypass-approvals-and-sandbox` must never appear in the codebase.**
- **Codex binary discovery order:** `$PATH` → `/Applications/ChatGPT.app/Contents/Resources/codex` → explicit config override.
- **Codex model aliases:** `sol` → `gpt-5.6-sol`, `luna` → `gpt-5.6-luna`.
- **Codex exec flags (verified 2026-08-25):** `--json`, `--output-schema <FILE>`, `-m <MODEL>`, `-C <DIR>`, `-s workspace-write`, `-o <FILE>`.
- **Claude flags (verified 2026-08-25):** `-p`, `--output-format json`, `--allowedTools`, `--model`.
- **Validation-command deny-list tokens:** `deploy`, `wrangler`, `publish`, `push`, `merge`, `release`. Not overridable by repository configuration.
- **Run ID format:** `YYYYMMDD-HHMMSS_<workflow>_<slug>`. **Branch format:** `ai/<run-id>-<slug>`.
- **Worktree root:** `~/.dev-orchestration/worktrees/<repo>/<run-id>/`.
- **Run artifacts:** `.ai/runs/<run-id>/` inside the managed repository.
- TDD throughout. Commit after each task. Never push.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, entry point, pytest/ruff config |
| `src/dev_orchestration/cli.py` | Typer app; command registration only, no logic |
| `src/dev_orchestration/domain/enums.py` | `Tier`, `RunState`, `Outcome`, `ProjectClass` |
| `src/dev_orchestration/domain/run.py` | `RunManifest` and its nested blocks |
| `src/dev_orchestration/config/models.py` | Pydantic config models + deny-list enforcement |
| `src/dev_orchestration/config/resolver.py` | Six-layer precedence, stricter-wins, protected keys |
| `src/dev_orchestration/scope.py` | The scope fence — glob matching and violation detection |
| `src/dev_orchestration/git/repo.py` | Discovery, clean guard, commits, branches |
| `src/dev_orchestration/git/worktree.py` | Worktree create/remove and path generation |
| `src/dev_orchestration/git/guards.py` | Prohibited operations; raise-only |
| `src/dev_orchestration/artifacts/store.py` | Run directory layout, append-only plan versions |
| `src/dev_orchestration/artifacts/events.py` | `events.jsonl` append |
| `src/dev_orchestration/adapters/base.py` | `AgentAdapter` protocol, request/result/status types |
| `src/dev_orchestration/adapters/codex.py` | Codex discovery, capability detection, exec |
| `src/dev_orchestration/adapters/claude.py` | Claude print-mode invocation |
| `src/dev_orchestration/adapters/registry.py` | Logical role → adapter + model alias |
| `src/dev_orchestration/doctor.py` | Environment checks and their rendering |
| `src/dev_orchestration/init_repo.py` | Writing the repository contract files |

---

### Task 1: Package skeleton and CLI entry point

**Files:**
- Create: `pyproject.toml`, `src/dev_orchestration/__init__.py`, `src/dev_orchestration/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `dev_orchestration.cli.app` — a `typer.Typer` instance that later tasks register commands on via `@app.command()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from typer.testing import CliRunner

from dev_orchestration.cli import app


def test_help_exits_zero():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dev_orchestration'`

- [ ] **Step 3: Write pyproject.toml**

```toml
[project]
name = "dev-orchestration"
version = "0.1.0"
description = "Local-first, vendor-neutral orchestration for AI-assisted development"
requires-python = ">=3.11"
dependencies = ["typer>=0.12", "pydantic>=2.7", "pyyaml>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.6"]

[project.scripts]
dev-orch = "dev_orchestration.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dev_orchestration"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: hits a real provider CLI; skipped by default"]
addopts = "-m 'not live'"

[tool.ruff]
line-length = 100
```

- [ ] **Step 4: Write the CLI module**

```python
# src/dev_orchestration/cli.py
"""Typer entry point. Commands register here; logic lives in modules."""

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first orchestration for AI-assisted development.",
)
```

Create `src/dev_orchestration/__init__.py` containing only `__version__ = "0.1.0"`.

- [ ] **Step 5: Install and run the test**

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/test_cli.py -v
```
Expected: PASS. Also verify `.venv/bin/dev-orch --help` exits 0.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src tests .gitignore
git commit -m "feat: package skeleton and dev-orch CLI entry point"
```

---

### Task 2: Domain enums and the run manifest

**Files:**
- Create: `src/dev_orchestration/domain/__init__.py`, `src/dev_orchestration/domain/enums.py`, `src/dev_orchestration/domain/run.py`
- Test: `tests/domain/test_run.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Tier`, `RunState`, `Outcome`, `ProjectClass` (all `str`-valued enums); `RunManifest`, `GitBlock`, `RoleBinding` (Pydantic models). `RunManifest.model_dump(mode="json")` round-trips through `RunManifest.model_validate`.

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_run.py
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import GitBlock, RunManifest


def test_manifest_round_trips_through_json():
    manifest = RunManifest(
        run_id="20260825-013500_feature_session-reconciliation",
        repository="helmfast-OS",
        workflow="feature",
        tier=Tier.STANDARD,
        project_class=ProjectClass.INTERNAL_OPERATING_SYSTEM,
        active_profiles=["canonical_knowledge"],
    )
    restored = RunManifest.model_validate(manifest.model_dump(mode="json"))
    assert restored == manifest


def test_manifest_starts_in_created_state():
    manifest = RunManifest(
        run_id="20260825-013500_feature_x",
        repository="r",
        workflow="feature",
        tier=Tier.TRIVIAL,
        project_class=ProjectClass.MARKETING_WEBSITE,
    )
    assert manifest.status is RunState.CREATED
    assert manifest.git == GitBlock()
    assert manifest.plan_origin == "generated"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_run.py -v`
Expected: FAIL — no module `dev_orchestration.domain.enums`

- [ ] **Step 3: Write the enums**

```python
# src/dev_orchestration/domain/enums.py
from enum import StrEnum


class Tier(StrEnum):
    TRIVIAL = "trivial"
    STANDARD = "standard"
    SUBSTANTIAL = "substantial"
    HIGH_RISK = "high_risk"


class ProjectClass(StrEnum):
    CLIENT_APPLICATION = "client_application"
    INTERNAL_UTILITY = "internal_utility"
    INTERNAL_OPERATING_SYSTEM = "internal_operating_system"
    MARKETING_WEBSITE = "marketing_website"


class RunState(StrEnum):
    CREATED = "CREATED"
    CLASSIFIED = "CLASSIFIED"
    PLANNED = "PLANNED"
    PLAN_REVIEWED = "PLAN_REVIEWED"
    PLAN_FINALIZED = "PLAN_FINALIZED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    IMPLEMENTATION_REVIEW = "IMPLEMENTATION_REVIEW"
    REMEDIATION = "REMEDIATION"
    FINAL_VERIFICATION = "FINAL_VERIFICATION"
    COMPLETE_LOCAL = "COMPLETE_LOCAL"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Outcome(StrEnum):
    PASS = "PASS"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"
```

- [ ] **Step 4: Write the run models**

```python
# src/dev_orchestration/domain/run.py
from datetime import datetime

from pydantic import BaseModel, Field

from dev_orchestration.domain.enums import ProjectClass, RunState, Tier


class GitBlock(BaseModel):
    base_commit: str | None = None
    branch: str | None = None
    worktree: str | None = None
    final_commit: str | None = None


class RoleBinding(BaseModel):
    adapter: str
    model_alias: str | None = None
    reasoning: str | None = None
    resolved_binary: str | None = None
    resolved_version: str | None = None


class RunManifest(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    repository: str
    workflow: str
    tier: Tier
    project_class: ProjectClass
    active_profiles: list[str] = Field(default_factory=list)
    plan_origin: str = "generated"
    roles: dict[str, RoleBinding] = Field(default_factory=dict)
    git: GitBlock = Field(default_factory=GitBlock)
    resolved_config: dict = Field(default_factory=dict)
    status: RunState = RunState.CREATED
    created_at: datetime | None = None
    completed_at: datetime | None = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/domain -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/dev_orchestration/domain tests/domain
git commit -m "feat(domain): run states, tiers, and the run manifest"
```

---

### Task 3: Configuration models and the validation deny-list

**Files:**
- Create: `src/dev_orchestration/config/__init__.py`, `src/dev_orchestration/config/models.py`
- Test: `tests/config/test_models.py`

**Interfaces:**
- Consumes: `Tier`, `ProjectClass` from Task 2.
- Produces: `Scope(include, exclude)`, `ValidationCommand(command, required_for)`, `ProjectConfig`, `GlobalConfig`, `DeniedCommandError`, and `denied_tokens(command) -> set[str]`.

**Why this exists:** `helmfast-site` ships `npm run deploy` → `wrangler deploy`. Nothing otherwise stops that being registered as a validation command, at which point the orchestrator deploys to production believing it is running a build.

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_models.py
import pytest
from pydantic import ValidationError

from dev_orchestration.config.models import (
    ProjectConfig,
    Scope,
    ValidationCommand,
    denied_tokens,
)
from dev_orchestration.domain.enums import Tier


def test_safe_command_is_accepted():
    cmd = ValidationCommand(command="npm run build", required_for=[Tier.STANDARD])
    assert cmd.command == "npm run build"


@pytest.mark.parametrize(
    "command",
    [
        "npm run deploy",
        "wrangler deploy",
        "npm run deploy:prod",
        "git push origin main",
        "npm publish",
        "gh release create",
    ],
)
def test_dangerous_commands_are_rejected(command):
    with pytest.raises(ValidationError):
        ValidationCommand(command=command)


def test_denied_tokens_reports_which_word_tripped():
    assert denied_tokens("npm run deploy:prod") == {"deploy"}
    assert denied_tokens("npm run build") == set()


def test_repo_config_cannot_smuggle_a_denied_command():
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(
            {
                "project": {"name": "helmfast-site", "class": "marketing_website"},
                "validation": {"build": {"command": "npm run deploy"}},
            }
        )


def test_scope_defaults_to_whole_repo():
    assert Scope().include == ["**"]
    assert Scope().exclude == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/config/test_models.py -v`
Expected: FAIL — no module `dev_orchestration.config.models`

- [ ] **Step 3: Write the models**

```python
# src/dev_orchestration/config/models.py
"""Typed configuration. Invalid config must fail before any agent runs."""

import re

from pydantic import BaseModel, Field, field_validator

from dev_orchestration.domain.enums import ProjectClass, Tier

DENIED_COMMAND_TOKENS = frozenset(
    {"deploy", "wrangler", "publish", "push", "merge", "release"}
)


class DeniedCommandError(ValueError):
    """A configured command matched the non-overridable deny-list."""


def denied_tokens(command: str) -> set[str]:
    """Return the deny-list tokens present in `command`.

    Splits on every non-alphanumeric run so `deploy:prod` and `predeploy`
    are both reduced to comparable words. Fails closed by design: a false
    positive costs one renamed script, a false negative deploys production.
    """
    words = set(re.split(r"[^a-z0-9]+", command.lower()))
    return words & DENIED_COMMAND_TOKENS


class ValidationCommand(BaseModel):
    command: str
    required_for: list[Tier] = Field(default_factory=list)
    timeout_seconds: int = 1800

    @field_validator("command")
    @classmethod
    def reject_denied_commands(cls, value: str) -> str:
        found = denied_tokens(value)
        if found:
            raise DeniedCommandError(
                f"command {value!r} contains prohibited token(s) "
                f"{sorted(found)}; validation commands may not deploy, "
                f"publish, or write to a remote"
            )
        return value


class Scope(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=list)


class ProjectMeta(BaseModel):
    name: str
    project_class: ProjectClass = Field(alias="class")

    model_config = {"populate_by_name": True}


class Profiles(BaseModel):
    available: list[str] = Field(default_factory=list)


class GitPolicy(BaseModel):
    branch_prefix: str = "ai/"
    autonomous_local_commits: bool = True
    autonomous_push: bool = False


class ProjectConfig(BaseModel):
    schema_version: str = "1.0"
    project: ProjectMeta
    profiles: Profiles = Field(default_factory=Profiles)
    scope: Scope = Field(default_factory=Scope)
    validation: dict[str, ValidationCommand] = Field(default_factory=dict)
    git: GitPolicy = Field(default_factory=GitPolicy)


class RoleConfig(BaseModel):
    adapter: str
    model: str | None = None
    reasoning: str | None = None


class GlobalConfig(BaseModel):
    schema_version: str = "1.0"
    worktree_root: str = "~/.dev-orchestration/worktrees"
    codex_binary: str | None = None
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/config -v`
Expected: PASS (9 tests including parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/config tests/config
git commit -m "feat(config): typed config models with validation deny-list"
```

---

### Task 4: The six-layer configuration resolver

**Files:**
- Create: `src/dev_orchestration/config/resolver.py`
- Test: `tests/config/test_resolver.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (operates on plain dicts).
- Produces: `resolve_policy(layers: list[dict]) -> dict` returning a flat dotted-key dict, and `PROTECTED_DEFAULTS: dict[str, bool]`.

**Rules, in order:** later layers override earlier ones; any key whose final segment ends in `_required` is OR-merged so a stricter layer wins regardless of position; the four `protected.*` keys are forced to their framework values last and cannot be weakened by any layer.

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_resolver.py
from dev_orchestration.config.resolver import PROTECTED_DEFAULTS, resolve_policy


def test_later_layer_wins_for_ordinary_keys():
    resolved = resolve_policy([{"workflow": {"max_remediation_cycles": 2}},
                               {"workflow": {"max_remediation_cycles": 5}}])
    assert resolved["workflow.max_remediation_cycles"] == 5


def test_stricter_requirement_wins_regardless_of_order():
    profile = {"planning": {"plan_review_required": True}}
    repo = {"planning": {"plan_review_required": False}}
    assert resolve_policy([profile, repo])["planning.plan_review_required"] is True
    assert resolve_policy([repo, profile])["planning.plan_review_required"] is True


def test_repo_cannot_weaken_protected_rules():
    rogue = {"protected": {"autonomous_push": True, "autonomous_deploy": True}}
    resolved = resolve_policy([rogue])
    assert resolved["protected.autonomous_push"] is False
    assert resolved["protected.autonomous_deploy"] is False


def test_protected_defaults_are_all_false():
    assert set(PROTECTED_DEFAULTS.values()) == {False}


def test_nested_keys_flatten_to_dotted_paths():
    resolved = resolve_policy([{"git": {"isolated_worktree": True}}])
    assert resolved["git.isolated_worktree"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/config/test_resolver.py -v`
Expected: FAIL — no module `dev_orchestration.config.resolver`

- [ ] **Step 3: Write the resolver**

```python
# src/dev_orchestration/config/resolver.py
"""Deterministic configuration resolution.

Layer order, lowest precedence first:
  framework defaults -> project class -> active profiles
  -> repo config -> workflow config -> CLI/run override
"""

from typing import Any

PROTECTED_DEFAULTS: dict[str, bool] = {
    "protected.autonomous_push": False,
    "protected.autonomous_merge": False,
    "protected.autonomous_deploy": False,
    "protected.autonomous_production_data_mutation": False,
}


def _flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=f"{path}."))
        else:
            flat[path] = value
    return flat


def _is_strictness_key(dotted: str) -> bool:
    return dotted.rsplit(".", 1)[-1].endswith("_required")


def resolve_policy(layers: list[dict]) -> dict[str, Any]:
    """Merge configuration layers into one flat dotted-key policy."""
    resolved: dict[str, Any] = {}
    for layer in layers:
        for key, value in _flatten(layer).items():
            if _is_strictness_key(key) and key in resolved:
                resolved[key] = bool(resolved[key]) or bool(value)
            else:
                resolved[key] = value
    resolved.update(PROTECTED_DEFAULTS)
    return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/config/resolver.py tests/config/test_resolver.py
git commit -m "feat(config): six-layer resolver with stricter-wins and protected rules"
```

---

### Task 5: The scope fence

**Files:**
- Create: `src/dev_orchestration/scope.py`
- Test: `tests/test_scope.py`

**Interfaces:**
- Consumes: `Scope` from Task 3.
- Produces: `ScopeFence(include: list[str], exclude: list[str])` with `.allows(rel_path: str) -> bool` and `.violations(paths: Iterable[str]) -> list[str]`; classmethod `ScopeFence.from_config(scope: Scope) -> ScopeFence`.

**Matching rules:** a pattern of `**` matches everything; a pattern ending in `/` matches that directory and everything beneath it; anything else is matched with `fnmatch`. A path is allowed when it matches at least one `include` **and** no `exclude`. Exclude always wins.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scope.py
from dev_orchestration.config.models import Scope
from dev_orchestration.scope import ScopeFence

HELMFAST_OS = ScopeFence(
    include=["pipeline/", "tests/", "planning/", "README.md"],
    exclude=["marketing/", "operations/", "market-research/", "strategy/",
             "context/", "data-templates/", "var/", ".venv/"],
)


def test_included_paths_are_allowed():
    assert HELMFAST_OS.allows("pipeline/runtime/store.py")
    assert HELMFAST_OS.allows("tests/runtime/test_store.py")
    assert HELMFAST_OS.allows("README.md")


def test_business_knowledge_is_fenced_out():
    assert not HELMFAST_OS.allows("strategy/icp-definition.md")
    assert not HELMFAST_OS.allows("marketing/brand/visual-identity.md")
    assert not HELMFAST_OS.allows("market-research/prospect-lists/README.md")


def test_paths_outside_include_are_denied_even_without_exclude():
    assert not HELMFAST_OS.allows("SOURCE-MANIFEST.md")


def test_exclude_beats_include():
    fence = ScopeFence(include=["**"], exclude=["secrets/"])
    assert fence.allows("src/app.py")
    assert not fence.allows("secrets/keys.json")


def test_violations_lists_every_offending_path():
    changed = ["pipeline/runtime/store.py", "strategy/pricing.md", "var/cache.db"]
    assert HELMFAST_OS.violations(changed) == ["strategy/pricing.md", "var/cache.db"]


def test_from_config_builds_an_equivalent_fence():
    fence = ScopeFence.from_config(Scope(include=["src/"], exclude=["src/vendor/"]))
    assert fence.allows("src/main.py")
    assert not fence.allows("src/vendor/lib.py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scope.py -v`
Expected: FAIL — no module `dev_orchestration.scope`

- [ ] **Step 3: Write the fence**

```python
# src/dev_orchestration/scope.py
"""The scope fence: which paths agents may modify.

Governs modification, not reference. Repository code may legitimately read
excluded trees (helmfast-OS's pipeline/runtime/prompts.py loads templates
from three of them); the fence only decides what a run may change.
"""

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass

from dev_orchestration.config.models import Scope


def _matches(pattern: str, rel_path: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/"):
        return rel_path == pattern.rstrip("/") or rel_path.startswith(pattern)
    return fnmatch.fnmatch(rel_path, pattern)


@dataclass(frozen=True)
class ScopeFence:
    include: list[str]
    exclude: list[str]

    @classmethod
    def from_config(cls, scope: Scope) -> "ScopeFence":
        return cls(include=list(scope.include), exclude=list(scope.exclude))

    def allows(self, rel_path: str) -> bool:
        normalized = rel_path.lstrip("./")
        if any(_matches(p, normalized) for p in self.exclude):
            return False
        return any(_matches(p, normalized) for p in self.include)

    def violations(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if not self.allows(p)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scope.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/scope.py tests/test_scope.py
git commit -m "feat(scope): hard fence for agent-modifiable paths"
```

---

### Task 6: Git repository operations and the clean-tree guard

**Files:**
- Create: `src/dev_orchestration/git/__init__.py`, `src/dev_orchestration/git/repo.py`
- Test: `tests/git/test_repo.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `GitRepo(root: Path)` with `.status_porcelain()`, `.ensure_clean()`, `.current_branch()`, `.current_commit()`, `.create_branch(name)`, `.commit(message, paths)`, `.changed_files(base_ref)`; plus `DirtyWorktreeError` and `GitCommandError`. `discover_repo(start: Path) -> GitRepo` walks upward to the repository root.

- [ ] **Step 1: Write the failing test**

```python
# tests/git/test_repo.py
import subprocess

import pytest

from dev_orchestration.git.repo import DirtyWorktreeError, GitRepo, discover_repo


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(tmp_path)


def test_clean_repo_passes_the_guard(repo):
    repo.ensure_clean()


def test_dirty_repo_raises_and_names_the_files(repo):
    (repo.root / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(DirtyWorktreeError) as excinfo:
        repo.ensure_clean()
    assert "dirty.txt" in str(excinfo.value)


def test_current_commit_is_a_full_sha(repo):
    assert len(repo.current_commit()) == 40


def test_create_branch_switches_to_it(repo):
    repo.create_branch("ai/20260825-013500_feature_x")
    assert repo.current_branch() == "ai/20260825-013500_feature_x"


def test_commit_records_only_named_paths(repo):
    (repo.root / "a.txt").write_text("a\n")
    (repo.root / "b.txt").write_text("b\n")
    sha = repo.commit("ai(run): add a", ["a.txt"])
    assert len(sha) == 40
    assert "b.txt" in repo.status_porcelain()


def test_changed_files_lists_paths_since_base(repo):
    base = repo.current_commit()
    (repo.root / "c.txt").write_text("c\n")
    repo.commit("ai(run): add c", ["c.txt"])
    assert repo.changed_files(base) == ["c.txt"]


def test_discover_repo_walks_up_from_a_subdirectory(repo):
    nested = repo.root / "deep" / "nested"
    nested.mkdir(parents=True)
    assert discover_repo(nested).root == repo.root
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/git/test_repo.py -v`
Expected: FAIL — no module `dev_orchestration.git.repo`

- [ ] **Step 3: Write the module**

```python
# src/dev_orchestration/git/repo.py
"""Git operations. Every call uses an argument array; none reach a remote."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 120


class GitCommandError(RuntimeError):
    """A git invocation exited non-zero."""


class DirtyWorktreeError(RuntimeError):
    """The base repository has uncommitted changes."""


@dataclass(frozen=True)
class GitRepo:
    root: Path

    def run_git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def status_porcelain(self) -> str:
        return self.run_git("status", "--porcelain")

    def ensure_clean(self) -> None:
        status = self.status_porcelain()
        if status:
            files = "\n  ".join(line[3:] for line in status.splitlines())
            raise DirtyWorktreeError(
                f"{self.root} has uncommitted changes:\n  {files}\n"
                "Commit or stash them before starting a run. "
                "dev-orch never modifies uncommitted work."
            )

    def current_branch(self) -> str:
        return self.run_git("rev-parse", "--abbrev-ref", "HEAD")

    def current_commit(self) -> str:
        return self.run_git("rev-parse", "HEAD")

    def create_branch(self, name: str) -> None:
        self.run_git("checkout", "-q", "-b", name)

    def commit(self, message: str, paths: list[str]) -> str:
        self.run_git("add", "--", *paths)
        self.run_git("commit", "-q", "-m", message, "--", *paths)
        return self.current_commit()

    def changed_files(self, base_ref: str) -> list[str]:
        output = self.run_git("diff", "--name-only", base_ref, "HEAD")
        return [line for line in output.splitlines() if line]


def discover_repo(start: Path) -> GitRepo:
    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise GitCommandError(f"{start} is not inside a git repository")
    return GitRepo(Path(proc.stdout.strip()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/git -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/git tests/git
git commit -m "feat(git): repository operations and clean-tree guard"
```

---

### Task 7: Worktree isolation and prohibited-operation guards

**Files:**
- Create: `src/dev_orchestration/git/worktree.py`, `src/dev_orchestration/git/guards.py`
- Test: `tests/git/test_worktree.py`, `tests/git/test_guards.py`

**Interfaces:**
- Consumes: `GitRepo`, `GitCommandError` from Task 6.
- Produces: `worktree_path(worktree_root, repo_name, run_id) -> Path`; `create_worktree(repo, path, branch) -> Path`; `remove_worktree(repo, path) -> None`; and in `guards.py` the raise-only `push()`, `merge()`, `deploy()` plus `ProhibitedOperationError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/git/test_worktree.py
import subprocess
from pathlib import Path

import pytest

from dev_orchestration.git.repo import GitRepo
from dev_orchestration.git.worktree import create_worktree, remove_worktree, worktree_path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(root)


def test_worktree_path_follows_the_documented_layout():
    path = worktree_path(Path("/wt"), "helmfast-OS", "20260825-013500_feature_x")
    assert path == Path("/wt/helmfast-OS/20260825-013500_feature_x")


def test_create_worktree_produces_an_isolated_checkout(repo, tmp_path):
    target = tmp_path / "wt" / "run1"
    created = create_worktree(repo, target, "ai/20260825-013500_feature_x")
    assert created.is_dir()
    assert (created / "README.md").read_text() == "hello\n"
    assert (repo.root / "README.md").exists()


def test_removing_a_worktree_leaves_the_base_repo_intact(repo, tmp_path):
    target = tmp_path / "wt" / "run2"
    create_worktree(repo, target, "ai/20260825-013500_feature_y")
    remove_worktree(repo, target)
    assert not target.exists()
    assert (repo.root / "README.md").exists()
```

```python
# tests/git/test_guards.py
from pathlib import Path

import pytest

from dev_orchestration.git import guards

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("operation", ["push", "merge", "deploy"])
def test_prohibited_operations_raise(operation):
    with pytest.raises(guards.ProhibitedOperationError):
        getattr(guards, operation)()


def test_no_module_invokes_a_remote_write():
    banned = ('"push"', "'push'", '"merge"', "'merge'", "force-with-lease")
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.name == "guards.py":
            continue
        text = path.read_text()
        offenders += [f"{path}: {token}" for token in banned if token in text]
    assert offenders == []


def test_the_sandbox_bypass_flag_appears_nowhere():
    for path in SOURCE_ROOT.rglob("*.py"):
        assert "dangerously-bypass" not in path.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/git/test_worktree.py tests/git/test_guards.py -v`
Expected: FAIL — no modules `worktree`, `guards`

- [ ] **Step 3: Write the guards**

```python
# src/dev_orchestration/git/guards.py
"""Operations dev-orchestration will not perform.

These exist so the prohibition is explicit and testable rather than merely
absent. V1 has no implementation of any of them.
"""


class ProhibitedOperationError(RuntimeError):
    """An operation forbidden in V1 was attempted."""


def push(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never pushes. Review the local branch and push it yourself."
    )


def merge(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never merges. Merge the local branch yourself."
    )


def deploy(*_args: object, **_kwargs: object) -> None:
    raise ProhibitedOperationError(
        "dev-orchestration never deploys. Deployment is a manual step in V1."
    )
```

- [ ] **Step 4: Write the worktree module**

```python
# src/dev_orchestration/git/worktree.py
"""Isolated worktrees. Agents only ever run inside one of these."""

from pathlib import Path

from dev_orchestration.git.repo import GitRepo


def worktree_path(worktree_root: Path, repo_name: str, run_id: str) -> Path:
    return worktree_root / repo_name / run_id


def create_worktree(repo: GitRepo, path: Path, branch: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    repo.run_git("worktree", "add", "-q", "-b", branch, str(path))
    return path


def remove_worktree(repo: GitRepo, path: Path) -> None:
    repo.run_git("worktree", "remove", "--force", str(path))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/git -v`
Expected: PASS (13 tests total)

- [ ] **Step 6: Commit**

```bash
git add src/dev_orchestration/git tests/git
git commit -m "feat(git): worktree isolation and prohibited-operation guards"
```

---

### Task 8: The run-artifact store

**Files:**
- Create: `src/dev_orchestration/artifacts/__init__.py`, `src/dev_orchestration/artifacts/store.py`, `src/dev_orchestration/artifacts/events.py`
- Test: `tests/artifacts/test_store.py`

**Interfaces:**
- Consumes: `RunManifest` from Task 2.
- Produces: `slugify(text) -> str`; `new_run_id(workflow, slug, now=None) -> str`; `RunStore(repo_root: Path, run_id: str)` with `.root`, `.initialize(manifest)`, `.write_plan_version(content) -> Path`, `.read_manifest() -> RunManifest`, `.update_manifest(**fields)`, `.append_event(event: dict)`; and `PlanOverwriteError`.

**Immutability rule:** `write_plan_version` never overwrites. It finds the highest existing `plan-vN.md` and writes `N+1`. Attempting to write a version file that already exists raises `PlanOverwriteError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/artifacts/test_store.py
import json
from datetime import datetime

import pytest

from dev_orchestration.artifacts.store import RunStore, new_run_id, slugify
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest


@pytest.fixture
def manifest():
    return RunManifest(
        run_id="20260825-013500_feature_session-reconciliation",
        repository="helmfast-OS",
        workflow="feature",
        tier=Tier.STANDARD,
        project_class=ProjectClass.INTERNAL_OPERATING_SYSTEM,
    )


@pytest.fixture
def store(tmp_path, manifest):
    s = RunStore(tmp_path, manifest.run_id)
    s.initialize(manifest)
    return s


def test_slugify_makes_a_filesystem_safe_token():
    assert slugify("Add Session Reconciliation!") == "add-session-reconciliation"


def test_run_id_follows_the_documented_format():
    when = datetime(2026, 8, 25, 1, 35, 0)
    assert new_run_id("feature", "session-reconciliation", now=when) == (
        "20260825-013500_feature_session-reconciliation"
    )


def test_initialize_creates_the_run_skeleton(store):
    for sub in ("planning", "execution", "review", "verification", "approval"):
        assert (store.root / sub).is_dir()
    assert (store.root / "manifest.json").is_file()


def test_plan_versions_are_append_only(store):
    first = store.write_plan_version("# Plan v1\n")
    second = store.write_plan_version("# Plan v2\n")
    assert first.name == "plan-v1.md"
    assert second.name == "plan-v2.md"
    assert first.read_text() == "# Plan v1\n"


def test_manifest_updates_persist(store):
    store.update_manifest(status=RunState.CLASSIFIED)
    assert store.read_manifest().status is RunState.CLASSIFIED


def test_events_append_one_json_object_per_line(store):
    store.append_event({"event": "STATE_CHANGED", "from": "CREATED", "to": "CLASSIFIED"})
    store.append_event({"event": "LOCAL_COMMIT_CREATED", "sha": "abc123"})
    lines = (store.root / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["sha"] == "abc123"
    assert "ts" in json.loads(lines[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/artifacts -v`
Expected: FAIL — no module `dev_orchestration.artifacts.store`

- [ ] **Step 3: Write the events module**

```python
# src/dev_orchestration/artifacts/events.py
"""Append-only machine-readable run timeline."""

import json
from datetime import UTC, datetime
from pathlib import Path


def append_event(path: Path, event: dict) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
```

- [ ] **Step 4: Write the store**

```python
# src/dev_orchestration/artifacts/store.py
"""Run artifacts under .ai/runs/<run-id>/.

Durable decision artifacts live here and are committed with the code they
explain. Plan versions are immutable once written.
"""

import re
from datetime import datetime
from pathlib import Path

from dev_orchestration.artifacts.events import append_event
from dev_orchestration.domain.run import RunManifest

SUBDIRECTORIES = ("planning", "execution", "review", "verification", "approval")


class PlanOverwriteError(RuntimeError):
    """An attempt was made to rewrite a reviewed plan version."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def new_run_id(workflow: str, slug: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/artifacts -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/dev_orchestration/artifacts tests/artifacts
git commit -m "feat(artifacts): run store with append-only plan versions"
```

---

### Task 9: Adapter protocol and the fake adapter

**Files:**
- Create: `src/dev_orchestration/adapters/__init__.py`, `src/dev_orchestration/adapters/base.py`
- Test: `tests/adapters/test_base.py`, `tests/adapters/conftest.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AgentRequest`, `AgentResult`, `AdapterStatus` (frozen dataclasses), the `AgentAdapter` protocol, and `FakeAdapter` — the test double every later integration test uses instead of a live provider.

`AgentRequest` fields: `role: str`, `prompt: str`, `cwd: Path`, `model_alias: str | None = None`, `reasoning: str | None = None`, `allowed_tools: list[str] | None = None`, `expected_schema: Path | None = None`, `timeout_seconds: int | None = None`.

`AgentResult` fields: `provider: str`, `model: str | None`, `exit_code: int`, `output: dict | str`, `started_at: datetime`, `completed_at: datetime`, `usage: dict | None = None`.

`AdapterStatus` fields: `name: str`, `available: bool`, `version: str | None`, `path: str | None`, `detail: str`, `capabilities: dict[str, bool]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/test_base.py
from pathlib import Path

from dev_orchestration.adapters.base import AgentAdapter, AgentRequest, FakeAdapter


def test_fake_adapter_satisfies_the_protocol():
    assert isinstance(FakeAdapter(), AgentAdapter)


def test_fake_adapter_returns_the_queued_response():
    adapter = FakeAdapter(responses=[{"verdict": "PASS"}])
    result = adapter.run(AgentRequest(role="plan_reviewer", prompt="review", cwd=Path(".")))
    assert result.output == {"verdict": "PASS"}
    assert result.exit_code == 0


def test_fake_adapter_records_every_request():
    adapter = FakeAdapter(responses=[{"a": 1}, {"b": 2}])
    adapter.run(AgentRequest(role="planner", prompt="one", cwd=Path(".")))
    adapter.run(AgentRequest(role="verifier", prompt="two", cwd=Path(".")))
    assert [r.role for r in adapter.requests] == ["planner", "verifier"]


def test_fake_adapter_healthcheck_reports_available():
    assert FakeAdapter().healthcheck().available is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/adapters -v`
Expected: FAIL — no module `dev_orchestration.adapters.base`

- [ ] **Step 3: Write the base module**

```python
# src/dev_orchestration/adapters/base.py
"""Provider-neutral agent interface.

Workflow code invokes roles through this protocol and never sees provider
command syntax. Adding a provider means adding an adapter, nothing else.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentRequest:
    role: str
    prompt: str
    cwd: Path
    model_alias: str | None = None
    reasoning: str | None = None
    allowed_tools: list[str] | None = None
    expected_schema: Path | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class AgentResult:
    provider: str
    model: str | None
    exit_code: int
    output: dict | str
    started_at: datetime
    completed_at: datetime
    usage: dict | None = None


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    available: bool
    version: str | None = None
    path: str | None = None
    detail: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


@runtime_checkable
class AgentAdapter(Protocol):
    def healthcheck(self) -> AdapterStatus: ...
    def run(self, request: AgentRequest) -> AgentResult: ...
    def supports(self, capability: str) -> bool: ...


class FakeAdapter:
    """Test double. Integration tests drive the engine through this."""

    def __init__(self, responses: list[dict | str] | None = None) -> None:
        self._responses = list(responses or [{}])
        self.requests: list[AgentRequest] = []

    def healthcheck(self) -> AdapterStatus:
        return AdapterStatus(name="fake", available=True, version="0", detail="test double")

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        payload = self._responses.pop(0) if self._responses else {}
        now = datetime.now(UTC)
        return AgentResult(
            provider="fake",
            model=request.model_alias,
            exit_code=0,
            output=payload,
            started_at=now,
            completed_at=now,
        )

    def supports(self, capability: str) -> bool:
        return capability in {"exec"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/adapters tests/adapters
git commit -m "feat(adapters): provider-neutral agent protocol and test double"
```

---

### Task 10: The Codex adapter

**Files:**
- Create: `src/dev_orchestration/adapters/codex.py`
- Test: `tests/adapters/test_codex.py`

**Interfaces:**
- Consumes: `AgentRequest`, `AgentResult`, `AdapterStatus` from Task 9.
- Produces: `CodexAdapter(binary: Path | None = None)` with `.healthcheck()`, `.build_exec_command(request) -> list[str]`, `.run(request)`, `.supports(capability)`; plus `discover_codex(override=None) -> Path | None`, `parse_version(text) -> str | None`, and `CODEX_BUNDLE_PATH`, `MODEL_ALIASES`.

**Capabilities reported:** `exec` (True when the binary exists), `goals_feature` (from `codex features list`), `goal_headless` (True only if `codex exec --help` advertises a goal flag — currently False on `0.146.0-alpha.9.2`).

**Do not invent syntax.** `goal_headless` is detected, never assumed.

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/test_codex.py
from pathlib import Path

from dev_orchestration.adapters.base import AgentRequest
from dev_orchestration.adapters.codex import (
    CODEX_BUNDLE_PATH,
    CodexAdapter,
    discover_codex,
    parse_version,
)


def test_parse_version_reads_the_installed_format():
    assert parse_version("codex-cli 0.146.0-alpha.9.2") == "0.146.0-alpha.9.2"
    assert parse_version("garbage") is None


def test_bundle_path_is_the_chatgpt_app_location():
    assert CODEX_BUNDLE_PATH == Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def test_discovery_prefers_an_explicit_override(tmp_path):
    override = tmp_path / "codex"
    override.write_text("#!/bin/sh\n")
    override.chmod(0o755)
    assert discover_codex(override=override) == override


def test_discovery_returns_none_when_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("dev_orchestration.adapters.codex.CODEX_BUNDLE_PATH", tmp_path / "absent")
    assert discover_codex() is None


def test_exec_command_uses_verified_flags():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    request = AgentRequest(
        role="planner",
        prompt="write a plan",
        cwd=Path("/work"),
        model_alias="sol",
        expected_schema=Path("/schemas/plan.json"),
    )
    cmd = adapter.build_exec_command(request)
    assert cmd[:2] == ["/bin/codex", "exec"]
    assert "--json" in cmd
    assert cmd[cmd.index("-C") + 1] == "/work"
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"
    assert cmd[cmd.index("--output-schema") + 1] == "/schemas/plan.json"
    assert cmd[-1] == "write a plan"


def test_exec_command_never_bypasses_the_sandbox():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    cmd = adapter.build_exec_command(
        AgentRequest(role="planner", prompt="p", cwd=Path("/w"))
    )
    assert not any("dangerously" in part for part in cmd)


def test_luna_alias_resolves_to_the_worker_model():
    adapter = CodexAdapter(binary=Path("/bin/codex"))
    cmd = adapter.build_exec_command(
        AgentRequest(role="implementation_worker", prompt="p", cwd=Path("/w"),
                     model_alias="luna")
    )
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-luna"


def test_healthcheck_reports_unavailable_without_a_binary():
    status = CodexAdapter(binary=None).healthcheck()
    assert status.available is False
    assert "not found" in status.detail.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/adapters/test_codex.py -v`
Expected: FAIL — no module `dev_orchestration.adapters.codex`

- [ ] **Step 3: Write the adapter**

```python
# src/dev_orchestration/adapters/codex.py
"""Codex adapter.

The binary is frequently not on PATH: it ships inside the ChatGPT desktop
application. Goal Mode exists as a stable feature but has no headless entry
point on the builds verified so far, so it is detected and reported, never
assumed.
"""

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.adapters.base import AdapterStatus, AgentRequest, AgentResult

CODEX_BUNDLE_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

MODEL_ALIASES = {"sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"}

DEFAULT_TIMEOUT_SECONDS = 3600


def parse_version(text: str) -> str | None:
    match = re.search(r"codex-cli\s+(\S+)", text)
    return match.group(1) if match else None


def discover_codex(override: Path | None = None) -> Path | None:
    """Resolve the codex binary: override -> PATH -> ChatGPT.app bundle."""
    if override is not None:
        return override if Path(override).exists() else None
    on_path = shutil.which("codex")
    if on_path:
        return Path(on_path)
    return CODEX_BUNDLE_PATH if CODEX_BUNDLE_PATH.exists() else None


class CodexAdapter:
    name = "codex"

    def __init__(self, binary: Path | None = None) -> None:
        self.binary = binary
        self._capabilities: dict[str, bool] | None = None

    def _probe(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.binary), *args], capture_output=True, text=True, timeout=timeout
        )

    def healthcheck(self) -> AdapterStatus:
        if self.binary is None:
            return AdapterStatus(
                name=self.name,
                available=False,
                detail=(
                    "codex not found on PATH or in "
                    f"{CODEX_BUNDLE_PATH}. Install the Codex CLI or set "
                    "codex_binary in ~/.dev-orchestration/config.yaml."
                ),
            )
        version = parse_version(self._probe("--version").stdout)
        return AdapterStatus(
            name=self.name,
            available=True,
            version=version,
            path=str(self.binary),
            detail=f"codex {version}",
            capabilities=self._detect_capabilities(),
        )

    def _detect_capabilities(self) -> dict[str, bool]:
        if self._capabilities is not None:
            return self._capabilities
        goals_feature = False
        goal_headless = False
        try:
            features = self._probe("features", "list").stdout
            goals_feature = bool(re.search(r"^goals\s+\S+\s+true", features, re.M))
            exec_help = self._probe("exec", "--help").stdout
            goal_headless = "--goal" in exec_help
        except (subprocess.SubprocessError, OSError):
            pass
        self._capabilities = {
            "exec": True,
            "goals_feature": goals_feature,
            "goal_headless": goal_headless,
        }
        return self._capabilities

    def supports(self, capability: str) -> bool:
        return self._detect_capabilities().get(capability, False)

    def build_exec_command(self, request: AgentRequest) -> list[str]:
        cmd = [
            str(self.binary),
            "exec",
            "--json",
            "-C",
            str(request.cwd),
            "-s",
            "workspace-write",
        ]
        if request.model_alias:
            cmd += ["-m", MODEL_ALIASES.get(request.model_alias, request.model_alias)]
        if request.expected_schema:
            cmd += ["--output-schema", str(request.expected_schema)]
        cmd.append(request.prompt)
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        proc = subprocess.run(
            self.build_exec_command(request),
            capture_output=True,
            text=True,
            cwd=request.cwd,
            timeout=request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        )
        return AgentResult(
            provider=self.name,
            model=MODEL_ALIASES.get(request.model_alias or "", request.model_alias),
            exit_code=proc.returncode,
            output=_last_json_object(proc.stdout) or proc.stdout,
            started_at=started,
            completed_at=datetime.now(UTC),
        )


def _last_json_object(stdout: str) -> dict | None:
    """codex exec --json emits JSONL; the final object carries the result."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters/test_codex.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/adapters/codex.py tests/adapters/test_codex.py
git commit -m "feat(adapters): codex adapter with discovery and capability detection"
```

---

### Task 11: The Claude adapter

**Files:**
- Create: `src/dev_orchestration/adapters/claude.py`
- Test: `tests/adapters/test_claude.py`

**Interfaces:**
- Consumes: `AgentRequest`, `AgentResult`, `AdapterStatus` from Task 9.
- Produces: `ClaudeAdapter(binary: str = "claude")` with `.healthcheck()`, `.build_command(request) -> list[str]`, `.run(request)`, `.supports(capability)`; plus `READ_ONLY_TOOLS`.

**Verified flags:** `-p`, `--output-format json`, `--allowedTools`, `--model`. Tools are passed as one comma-joined argument, and `-p <prompt>` goes last so a variadic flag cannot swallow the prompt.

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/test_claude.py
from pathlib import Path

from dev_orchestration.adapters.base import AgentRequest
from dev_orchestration.adapters.claude import READ_ONLY_TOOLS, ClaudeAdapter


def test_command_uses_print_mode_and_json_output():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt="review this", cwd=Path("/work"))
    )
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-2] == "-p"
    assert cmd[-1] == "review this"


def test_allowed_tools_are_comma_joined_in_one_argument():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(
            role="plan_reviewer",
            prompt="review",
            cwd=Path("/work"),
            allowed_tools=list(READ_ONLY_TOOLS),
        )
    )
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_model_alias_is_passed_through():
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt="r", cwd=Path("/w"), model_alias="opus")
    )
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_prompt_is_a_separate_argv_element_not_interpolated():
    injected = 'review"; rm -rf /'
    cmd = ClaudeAdapter().build_command(
        AgentRequest(role="plan_reviewer", prompt=injected, cwd=Path("/w"))
    )
    assert cmd[-1] == injected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/adapters/test_claude.py -v`
Expected: FAIL — no module `dev_orchestration.adapters.claude`

- [ ] **Step 3: Write the adapter**

```python
# src/dev_orchestration/adapters/claude.py
"""Claude Code adapter, driven in non-interactive print mode."""

import json
import shutil
import subprocess
from datetime import UTC, datetime

from dev_orchestration.adapters.base import AdapterStatus, AgentRequest, AgentResult

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")

DEFAULT_TIMEOUT_SECONDS = 1800


class ClaudeAdapter:
    name = "claude"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def healthcheck(self) -> AdapterStatus:
        path = shutil.which(self.binary)
        if path is None:
            return AdapterStatus(
                name=self.name,
                available=False,
                detail="claude not found on PATH. Install Claude Code.",
            )
        proc = subprocess.run(
            [self.binary, "--version"], capture_output=True, text=True, timeout=30
        )
        version = proc.stdout.strip() or None
        return AdapterStatus(
            name=self.name,
            available=True,
            version=version,
            path=path,
            detail=f"claude {version}",
            capabilities={"exec": True, "read_only_review": True},
        )

    def supports(self, capability: str) -> bool:
        return capability in {"exec", "read_only_review"}

    def build_command(self, request: AgentRequest) -> list[str]:
        cmd = [self.binary, "--output-format", "json"]
        if request.model_alias:
            cmd += ["--model", request.model_alias]
        if request.allowed_tools:
            cmd += ["--allowedTools", ",".join(request.allowed_tools)]
        cmd += ["-p", request.prompt]
        return cmd

    def run(self, request: AgentRequest) -> AgentResult:
        started = datetime.now(UTC)
        proc = subprocess.run(
            self.build_command(request),
            capture_output=True,
            text=True,
            cwd=request.cwd,
            timeout=request.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        )
        try:
            output: dict | str = json.loads(proc.stdout)
        except json.JSONDecodeError:
            output = proc.stdout
        return AgentResult(
            provider=self.name,
            model=request.model_alias,
            exit_code=proc.returncode,
            output=output,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters -v`
Expected: PASS (16 tests across all adapter tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/adapters/claude.py tests/adapters/test_claude.py
git commit -m "feat(adapters): claude print-mode adapter"
```

---

### Task 12: The role registry

**Files:**
- Create: `src/dev_orchestration/adapters/registry.py`
- Test: `tests/adapters/test_registry.py`

**Interfaces:**
- Consumes: `CodexAdapter` (Task 10), `ClaudeAdapter` (Task 11), `RoleConfig` (Task 3), `AgentAdapter` (Task 9).
- Produces: `DEFAULT_ROLES: dict[str, RoleConfig]` and `RoleRegistry(roles, adapters)` with `.adapter_for(role) -> AgentAdapter`, `.binding_for(role) -> RoleConfig`, `.build_request(role, prompt, cwd, **kwargs) -> AgentRequest`; plus `UnknownRoleError`.

**Why:** workflow code calls `registry.build_request("plan_reviewer", ...)`. It never names a model or a provider.

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/test_registry.py
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import FakeAdapter
from dev_orchestration.adapters.registry import (
    DEFAULT_ROLES,
    RoleRegistry,
    UnknownRoleError,
)


@pytest.fixture
def registry():
    return RoleRegistry(
        roles=DEFAULT_ROLES,
        adapters={"codex": FakeAdapter(), "claude": FakeAdapter()},
    )


def test_default_roles_match_the_specification():
    assert DEFAULT_ROLES["planner"].adapter == "codex"
    assert DEFAULT_ROLES["planner"].model == "sol"
    assert DEFAULT_ROLES["implementation_worker"].model == "luna"
    assert DEFAULT_ROLES["plan_reviewer"].adapter == "claude"
    assert DEFAULT_ROLES["implementation_reviewer"].adapter == "claude"
    assert DEFAULT_ROLES["verifier"].model == "sol"


def test_build_request_carries_the_configured_model(registry):
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.model_alias == "luna"
    assert request.role == "implementation_worker"


def test_review_roles_are_restricted_to_read_only_tools(registry):
    request = registry.build_request("plan_reviewer", prompt="review", cwd=Path("/w"))
    assert request.allowed_tools == ["Read", "Grep", "Glob"]


def test_worker_roles_are_not_tool_restricted(registry):
    request = registry.build_request("implementation_worker", prompt="build", cwd=Path("/w"))
    assert request.allowed_tools is None


def test_unknown_role_raises(registry):
    with pytest.raises(UnknownRoleError):
        registry.adapter_for("nonexistent_role")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/adapters/test_registry.py -v`
Expected: FAIL — no module `dev_orchestration.adapters.registry`

- [ ] **Step 3: Write the registry**

```python
# src/dev_orchestration/adapters/registry.py
"""Logical roles resolve to a provider adapter plus a model alias here.

Nowhere else in the codebase should a model name appear.
"""

from pathlib import Path

from dev_orchestration.adapters.base import AgentAdapter, AgentRequest
from dev_orchestration.adapters.claude import READ_ONLY_TOOLS
from dev_orchestration.config.models import RoleConfig

READ_ONLY_ROLES = frozenset({"plan_reviewer", "implementation_reviewer", "verifier"})

DEFAULT_ROLES: dict[str, RoleConfig] = {
    "classifier": RoleConfig(adapter="codex", model="luna", reasoning="medium"),
    "planner": RoleConfig(adapter="codex", model="sol", reasoning="high"),
    "plan_reviewer": RoleConfig(adapter="claude", model="opus"),
    "plan_reconciler": RoleConfig(adapter="codex", model="sol", reasoning="high"),
    "goal_executor": RoleConfig(adapter="codex", model="luna", reasoning="high"),
    "implementation_worker": RoleConfig(adapter="codex", model="luna", reasoning="high"),
    "implementation_reviewer": RoleConfig(adapter="claude", model="opus"),
    "verifier": RoleConfig(adapter="codex", model="sol", reasoning="high"),
}


class UnknownRoleError(KeyError):
    """A role was requested that has no configured binding."""


class RoleRegistry:
    def __init__(
        self, roles: dict[str, RoleConfig], adapters: dict[str, AgentAdapter]
    ) -> None:
        self.roles = roles
        self.adapters = adapters

    def binding_for(self, role: str) -> RoleConfig:
        try:
            return self.roles[role]
        except KeyError as exc:
            raise UnknownRoleError(f"no binding configured for role {role!r}") from exc

    def adapter_for(self, role: str) -> AgentAdapter:
        binding = self.binding_for(role)
        try:
            return self.adapters[binding.adapter]
        except KeyError as exc:
            raise UnknownRoleError(
                f"role {role!r} needs adapter {binding.adapter!r}, which is not registered"
            ) from exc

    def build_request(self, role: str, prompt: str, cwd: Path, **kwargs: object) -> AgentRequest:
        binding = self.binding_for(role)
        allowed = list(READ_ONLY_TOOLS) if role in READ_ONLY_ROLES else None
        return AgentRequest(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model_alias=binding.model,
            reasoning=binding.reasoning,
            allowed_tools=allowed,
            **kwargs,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dev_orchestration/adapters/registry.py tests/adapters/test_registry.py
git commit -m "feat(adapters): role registry keeps model names out of workflow code"
```

---

### Task 13: The `doctor` command

**Files:**
- Create: `src/dev_orchestration/doctor.py`
- Modify: `src/dev_orchestration/cli.py` (register the command)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `CodexAdapter`, `ClaudeAdapter`, `discover_codex`, `GitRepo`, `ScopeFence`, `ProjectConfig`.
- Produces: `Check(name: str, ok: bool, detail: str)` (frozen dataclass), `run_checks(cwd: Path) -> list[Check]`, `render(checks) -> str`, and a `doctor` Typer command.

**Output contract:** one line per check, `✓` when ok and `!` otherwise, detail appended after a dash. A failing check's detail must name the next action.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor.py
from dev_orchestration.doctor import Check, render


def test_render_marks_passing_and_failing_checks():
    output = render([
        Check(name="git available", ok=True, detail="2.39.5"),
        Check(name="repository clean", ok=False, detail="3 uncommitted files"),
    ])
    lines = output.splitlines()
    assert lines[0].startswith("✓ git available")
    assert lines[1].startswith("! repository clean")
    assert "3 uncommitted files" in lines[1]


def test_render_handles_an_empty_check_list():
    assert render([]) == ""


def test_check_is_immutable():
    check = Check(name="x", ok=True, detail="")
    try:
        check.ok = False
    except (AttributeError, TypeError):
        return
    raise AssertionError("Check should be frozen")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -v`
Expected: FAIL — no module `dev_orchestration.doctor`

- [ ] **Step 3: Write the doctor module**

```python
# src/dev_orchestration/doctor.py
"""Environment diagnosis. Every failure names its next action."""

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

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


def run_checks(cwd: Path) -> list[Check]:
    checks = [
        Check("macOS", platform.system() == "Darwin", platform.platform()),
        Check("python >= 3.11", sys.version_info >= (3, 11), platform.python_version()),
        Check("git available", shutil.which("git") is not None, _git_version()),
    ]

    codex_status = CodexAdapter(binary=discover_codex()).healthcheck()
    checks.append(Check("codex available", codex_status.available, codex_status.detail))
    if codex_status.available:
        caps = codex_status.capabilities
        checks.append(
            Check(
                "codex goal mode",
                caps.get("goal_headless", False),
                "goals feature enabled but no headless entry point; "
                "execution uses `codex exec`"
                if caps.get("goals_feature")
                else "not available",
            )
        )

    claude_status = ClaudeAdapter().healthcheck()
    checks.append(Check("claude available", claude_status.available, claude_status.detail))
    checks.extend(_repository_checks(cwd))
    return checks


def _git_version() -> str:
    if shutil.which("git") is None:
        return "install git"
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30)
    return proc.stdout.strip()


def _repository_checks(cwd: Path) -> list[Check]:
    try:
        repo = discover_repo(cwd)
    except GitCommandError:
        return [Check("git repository", False, f"{cwd} is not inside a git repository")]

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
        checks.append(
            Check("project config", False, "no .ai/project.yaml; run `dev-orch init`")
        )
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
    checks.append(
        Check(
            "AGENTS.md",
            (repo.root / "AGENTS.md").exists(),
            "missing; run `dev-orch init`",
        )
    )
    for name, command in config.validation.items():
        checks.append(Check(f"validation:{name}", True, command.command))
    return checks
```

- [ ] **Step 4: Register the command**

```python
# append to src/dev_orchestration/cli.py
from pathlib import Path

from dev_orchestration import doctor as doctor_module


@app.command()
def doctor() -> None:
    """Diagnose the local environment and repository contract."""
    typer.echo(doctor_module.render(doctor_module.run_checks(Path.cwd())))
```

- [ ] **Step 5: Run tests and the command**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -v` → PASS (3 tests)
Then run `.venv/bin/dev-orch doctor` in this repository and confirm it prints a check list including `codex available` and `claude available`.

- [ ] **Step 6: Commit**

```bash
git add src/dev_orchestration/doctor.py src/dev_orchestration/cli.py tests/test_doctor.py
git commit -m "feat(doctor): environment and repository contract checks"
```

---

### Task 14: The `init` command

**Files:**
- Create: `src/dev_orchestration/init_repo.py`
- Modify: `src/dev_orchestration/cli.py`
- Test: `tests/test_init_repo.py`

**Interfaces:**
- Consumes: `ProjectConfig`, `ProjectClass`, `GitRepo`.
- Produces: `render_project_yaml(config) -> str`, `render_agents_md(config) -> str`, `render_context_md(config) -> str`, `initialize_repo(repo_root, config, force=False) -> list[Path]`, and `FileExistsRefusal`.

**Additive-only rule:** `initialize_repo` writes only files that do not exist. Encountering an existing file without `force=True` raises `FileExistsRefusal` naming the path. It never moves, deletes, or edits existing content.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_init_repo.py
import pytest
import yaml

from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.init_repo import FileExistsRefusal, initialize_repo

CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "helmfast-site", "class": "marketing_website"},
        "scope": {"include": ["src/", "docs/"], "exclude": ["node_modules/"]},
        "validation": {"build": {"command": "npm run build"}},
    }
)


def test_initialize_writes_the_repository_contract(tmp_path):
    written = initialize_repo(tmp_path, CONFIG)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert names == {"AGENTS.md", ".ai/project.yaml", ".ai/context.md"}
    assert (tmp_path / ".ai" / "project.yaml").is_file()


def test_written_config_round_trips(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    loaded = yaml.safe_load((tmp_path / ".ai" / "project.yaml").read_text())
    restored = ProjectConfig.model_validate(loaded)
    assert restored.project.name == "helmfast-site"
    assert restored.scope.exclude == ["node_modules/"]


def test_initialize_refuses_to_overwrite(tmp_path):
    (tmp_path / "AGENTS.md").write_text("existing content\n")
    with pytest.raises(FileExistsRefusal) as excinfo:
        initialize_repo(tmp_path, CONFIG)
    assert "AGENTS.md" in str(excinfo.value)
    assert (tmp_path / "AGENTS.md").read_text() == "existing content\n"


def test_force_allows_regeneration(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    written = initialize_repo(tmp_path, CONFIG, force=True)
    assert len(written) == 3


def test_agents_md_states_the_scope_fence(tmp_path):
    initialize_repo(tmp_path, CONFIG)
    text = (tmp_path / "AGENTS.md").read_text()
    assert "node_modules/" in text
    assert "npm run build" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_init_repo.py -v`
Expected: FAIL — no module `dev_orchestration.init_repo`

- [ ] **Step 3: Write the module**

```python
# src/dev_orchestration/init_repo.py
"""Write the repository contract. Additive only: never moves or edits."""

from pathlib import Path

import yaml

from dev_orchestration.config.models import ProjectConfig


class FileExistsRefusal(FileExistsError):
    """A contract file already exists and force was not requested."""


def render_project_yaml(config: ProjectConfig) -> str:
    return yaml.safe_dump(
        config.model_dump(by_alias=True, mode="json"), sort_keys=False
    )


def render_agents_md(config: ProjectConfig) -> str:
    scope_in = "\n".join(f"- `{p}`" for p in config.scope.include)
    scope_out = "\n".join(f"- `{p}`" for p in config.scope.exclude) or "- (none)"
    commands = (
        "\n".join(f"- `{name}`: `{cmd.command}`" for name, cmd in config.validation.items())
        or "- (none configured)"
    )
    return f"""# AGENTS.md — {config.project.name}

Canonical agent instructions for this repository. Vendor-specific files
(`CLAUDE.md` and equivalents) are thin pointers to this document, not a
second source of truth.

## Project

- **Class:** `{config.project.project_class}`
- **Orchestration config:** `.ai/project.yaml`
- **Repository context:** `.ai/context.md`

## Scope

Agents may modify only these paths:

{scope_in}

Agents must never modify these paths:

{scope_out}

The fence governs modification, not reference: repository code may read
excluded paths legitimately.

## Validation commands

{commands}

Never register a command that deploys, publishes, or writes to a remote.

## Boundaries

- No pushes, merges, or deployments are ever performed automatically.
- Uncommitted work is never modified, stashed, or branched around.
- Changes crossing a security, privacy, data, payment, or architecture
  boundary stop and escalate rather than improvise.

## Open decision

Ownership of planning state between `.ai/runs/` (dev-orchestration) and
`.planning/` (GSD) is not yet settled. Until it is, treat `.ai/runs/` as the
record of what a dev-orchestration run did, and do not assume either system
enforces the other's boundaries.
"""


def render_context_md(config: ProjectConfig) -> str:
    return f"""# Repository context — {config.project.name}

Orchestration notes that belong neither in product nor architecture
documentation: local setup quirks, common pitfalls, generated-code
boundaries, and repository-specific escalation triggers.

_Populate during onboarding._
"""


def initialize_repo(
    repo_root: Path, config: ProjectConfig, force: bool = False
) -> list[Path]:
    targets = {
        repo_root / "AGENTS.md": render_agents_md(config),
        repo_root / ".ai" / "project.yaml": render_project_yaml(config),
        repo_root / ".ai" / "context.md": render_context_md(config),
    }
    if not force:
        for path in targets:
            if path.exists():
                raise FileExistsRefusal(
                    f"{path} already exists; dev-orch init never overwrites. "
                    "Move it aside or pass --force."
                )
    written = []
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
```

- [ ] **Step 4: Register the command**

Consolidate imports at the top of `cli.py` — `Path` and `typer` are already
imported from Task 13; add only what is new.

```python
# append to src/dev_orchestration/cli.py
import yaml

from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.git.repo import discover_repo
from dev_orchestration.init_repo import initialize_repo


@app.command()
def init(
    config_file: Path = typer.Option(..., "--config", help="Prepared .ai/project.yaml"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing contract files"),
) -> None:
    """Write the repository contract into the current repository."""
    repo = discover_repo(Path.cwd())
    config = ProjectConfig.model_validate(yaml.safe_load(config_file.read_text()))
    for path in initialize_repo(repo.root, config, force=force):
        typer.echo(f"wrote {path.relative_to(repo.root)}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_init_repo.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add src/dev_orchestration/init_repo.py src/dev_orchestration/cli.py tests/test_init_repo.py
git commit -m "feat(init): additive repository contract generation"
```

---

### Task 15: Onboard the three helmfast repositories

**Files:**
- Create: `helmfast-OS/.ai/project.yaml`, `helmfast-OS/.ai/context.md`, `helmfast-OS/AGENTS.md`
- Create: `helmfast-app/.ai/project.yaml`, `helmfast-app/.ai/context.md`, `helmfast-app/AGENTS.md`
- Create: `helmfast-site/.ai/project.yaml`, `helmfast-site/.ai/context.md`, `helmfast-site/AGENTS.md`
- Modify: `helmfast-OS/CLAUDE.md` — reduce to a pointer, preserving the original as `docs/archive/CLAUDE-original.md`

**Interfaces:**
- Consumes: `dev-orch init`, `dev-orch doctor` from Tasks 13–14.
- Produces: three onboarded repositories. No further code.

**Rules for this task:** work on a branch in each repository. Commit but never push. Do not touch `helmfast-OS`'s uncommitted `pipeline/ui/` or `tests/ui/` work — if `git status` shows it, leave it exactly as found and commit only the files this task creates, naming them explicitly in `git add`.

- [ ] **Step 1: Prove helmfast-OS's validation commands before configuring them**

```bash
cd /Users/andrewodonnell/GitRepos/helmfast/helmfast-OS
.venv/bin/python -m pytest --collect-only -q | tail -3
.venv/bin/python -m ruff --version
```

Expected on the environment verified 2026-08-25: pytest collects 639 tests; **ruff is not installed** (`No module named ruff`).

A command that does not run is not policy. Configure `test` only, and record ruff's absence in `.ai/context.md`. Do **not** add a `lint` entry.

- [ ] **Step 2: Write helmfast-OS's config to a scratch file**

```yaml
# /tmp/helmfast-os-project.yaml
schema_version: "1.0"
project:
  name: helmfast-OS
  class: internal_operating_system
profiles:
  available:
    - agent_orchestration
    - canonical_knowledge
    - external_api
    - customer_data
scope:
  include:
    - "pipeline/"
    - "tests/"
    - "planning/"
    - "README.md"
  exclude:
    - "marketing/"
    - "operations/"
    - "market-research/"
    - "strategy/"
    - "context/"
    - "data-templates/"
    - "var/"
    - ".venv/"
validation:
  test:
    command: ".venv/bin/python -m pytest"
    required_for: ["standard", "substantial", "high_risk"]
git:
  branch_prefix: "ai/"
  autonomous_local_commits: true
  autonomous_push: false
```

- [ ] **Step 3: Onboard helmfast-OS on a branch**

```bash
cd /Users/andrewodonnell/GitRepos/helmfast/helmfast-OS
git checkout -b ai/onboard-dev-orchestration
/Users/andrewodonnell/GitRepos/dev-orchestration/.venv/bin/dev-orch init --config /tmp/helmfast-os-project.yaml
```

Expected: three files written. If `init` refuses because `AGENTS.md` exists, stop and report — do not pass `--force` without checking what is there.

- [ ] **Step 4: Reduce CLAUDE.md to a pointer, preserving the original**

```bash
mkdir -p docs/archive
cp CLAUDE.md docs/archive/CLAUDE-original.md
```

Add this header to the top of `docs/archive/CLAUDE-original.md`:

```markdown
> Status: Archived
> Superseded by: ../../AGENTS.md
> Archived on: 2026-08-25
```

Then replace `CLAUDE.md` with:

```markdown
# Claude Code Instructions

The canonical repository instructions are in [`AGENTS.md`](AGENTS.md).
Read it before beginning work.

Orchestration configuration is `.ai/project.yaml`. The doc index that
previously lived here is preserved at
[`docs/archive/CLAUDE-original.md`](docs/archive/CLAUDE-original.md).

Do not create a second competing source of truth in this file.
```

- [ ] **Step 5: Record the ruff gap in context.md**

Append to `helmfast-OS/.ai/context.md`:

```markdown
## Validation notes

`ruff` is configured nowhere and is not installed in `.venv` — only a stale
`.ruff_cache/` remains from an earlier run. Linting is therefore **not** a
registered validation command. Install ruff and prove `ruff check` runs
before adding a `lint` entry to `.ai/project.yaml`.

`pytest` runs via `.venv/bin/python -m pytest` and collected 639 tests on
2026-08-25.
```

- [ ] **Step 6: Commit helmfast-OS, naming files explicitly**

```bash
git add AGENTS.md .ai/project.yaml .ai/context.md CLAUDE.md docs/archive/CLAUDE-original.md
git commit -m "chore: onboard to dev-orchestration repository contract"
git status --short
```

Expected: the commit contains exactly those five paths, and `git status` still shows the pre-existing `pipeline/ui/`, `tests/ui/`, and modified pipeline files untouched.

- [ ] **Step 7: Onboard helmfast-app**

```yaml
# /tmp/helmfast-app-project.yaml
schema_version: "1.0"
project:
  name: helmfast-app
  class: client_application
scope:
  include:
    - "docs/"
    - "README.md"
  exclude: []
validation: {}
git:
  branch_prefix: "ai/"
  autonomous_local_commits: true
  autonomous_push: false
```

```bash
cd /Users/andrewodonnell/GitRepos/helmfast/helmfast-app
git checkout -b ai/onboard-dev-orchestration
/Users/andrewodonnell/GitRepos/dev-orchestration/.venv/bin/dev-orch init --config /tmp/helmfast-app-project.yaml
git add AGENTS.md .ai/project.yaml .ai/context.md
git commit -m "chore: onboard to dev-orchestration repository contract"
```

- [ ] **Step 8: Prove helmfast-site's build command, then onboard**

```bash
cd /Users/andrewodonnell/GitRepos/helmfast/helmfast-site
npm run build
```

Expected: `astro check && astro build` completes with exit code 0. If it fails, record the failure in `.ai/context.md` and configure no `build` entry — an unproven command does not become policy.

```yaml
# /tmp/helmfast-site-project.yaml
schema_version: "1.0"
project:
  name: helmfast-site
  class: marketing_website
scope:
  include:
    - "src/"
    - "docs/"
    - "scripts/"
    - "public/"
    - "astro.config.mjs"
    - "package.json"
    - "tsconfig.json"
    - "README.md"
  exclude:
    - "node_modules/"
    - "dist/"
    - ".astro/"
    - ".wrangler/"
validation:
  build:
    command: "npm run build"
    required_for: ["standard", "substantial", "high_risk"]
git:
  branch_prefix: "ai/"
  autonomous_local_commits: true
  autonomous_push: false
```

```bash
git checkout -b ai/onboard-dev-orchestration
/Users/andrewodonnell/GitRepos/dev-orchestration/.venv/bin/dev-orch init --config /tmp/helmfast-site-project.yaml
git add AGENTS.md .ai/project.yaml .ai/context.md
git commit -m "chore: onboard to dev-orchestration repository contract"
```

Add to `helmfast-site/.ai/context.md`:

```markdown
## Deployment boundary

`npm run deploy` (`wrangler deploy`) publishes to Cloudflare. It is on the
non-overridable validation deny-list and must never be registered as a
validation command or invoked by any run. Deployment is manual.
```

- [ ] **Step 9: Verify all three with doctor**

```bash
for repo in helmfast-OS helmfast-app helmfast-site; do
  echo "=== $repo ==="
  (cd /Users/andrewodonnell/GitRepos/helmfast/$repo && \
   /Users/andrewodonnell/GitRepos/dev-orchestration/.venv/bin/dev-orch doctor)
done
```

Expected: each prints its project class, scope fence, and validation commands.
`helmfast-OS` must show `! repository clean` naming the pre-existing uncommitted
files — that is the guard working, not a failure to fix.

- [ ] **Step 10: Record the outcome and commit the framework repo**

Append an "M1 outcome" section to the design document noting: the ruff gap,
whether `npm run build` passed, and any check that failed. Then:

```bash
cd /Users/andrewodonnell/GitRepos/dev-orchestration
git add docs/superpowers/specs/2026-08-25-dev-orchestration-v1-design.md
git commit -m "docs(design): record M1 onboarding outcome"
```

---

## M1 Acceptance Verification

Run before declaring M1 complete. Every line must be evidenced by output, not asserted.

```bash
cd /Users/andrewodonnell/GitRepos/dev-orchestration
.venv/bin/python -m pytest -v
.venv/bin/python -m ruff check src tests
.venv/bin/dev-orch --help
.venv/bin/dev-orch doctor
```

- [ ] `dev-orch --help` and `dev-orch doctor` succeed on macOS
- [ ] Invalid configuration fails before any agent is invoked (Task 3 tests)
- [ ] A deny-listed validation command fails config load (Task 3 tests)
- [ ] Stricter profile policy beats weaker class policy (Task 4 tests)
- [ ] Dirty repository blocks and names the files (Task 6 tests + live helmfast-OS)
- [ ] Worktree creation, local commit, and cleanup work (Tasks 6–7 tests)
- [ ] No push/merge/deploy code path exists (Task 7 source-scanning test)
- [ ] `doctor` reports Codex path, version, and goal-capability state
- [ ] All three pilot repos carry `AGENTS.md`, `.ai/project.yaml`, `.ai/context.md`
- [ ] `helmfast-OS/CLAUDE.md` is a pointer; the original is archived; no existing file moved
- [ ] `helmfast-OS`'s uncommitted `pipeline/ui/` work is byte-identical to before onboarding
