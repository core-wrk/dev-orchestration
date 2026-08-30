# M2 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four blocking defects, seven major defects, and four minor defects found in the M2 implementation review, so that no run can reach `COMPLETE_LOCAL` without a real, in-scope, independently verified change — and so the acceptance evidence names a commit that actually contains the code.

**Architecture:** Every fix tightens an existing seam rather than adding a layer. Three of the four blockers are the same mistake in different places — the terminal gate trusts a *shape* (an empty commit is still a commit, an empty verdict list is still unanimous, an invisible file is still no file) instead of a *fact* — so each is fixed by binding the gate to the run's own recorded ground truth: the change inventory, the supplied acceptance criteria, and a git inventory that can see ignored paths. The remaining work wires configuration that already exists but is discarded, and puts a hard budget on the prompt so a real diff cannot blow past `MAX_ARG_STRLEN`.

**Tech Stack:** Python 3.13+, Pydantic v2, Typer, pytest, ruff, hatchling. No new dependencies.

**Spec:** `docs/superpowers/plans/2026-08-29-m2-implementation-review.md` (findings B1–B4, D1–D7, E1–E4)

**Prior contract:** `docs/superpowers/plans/2026-08-29-m2-the-loop-adversarial-review-2026-08-29.md` (acceptance checks A1–A17, which this plan must leave passing and re-evidence)

## Global Constraints

- Repository: `/Users/andrewodonnell/GitRepos/dev-orchestration`. All paths below are relative to it.
- Python floor becomes `>=3.13` (Task 5). Do not use any API newer than 3.13.
- Interpreter for every command: `.venv/bin/python`. Never the system Python.
- `ruff check .` and `ruff format --check src tests` must pass at the end of **every** task.
- Line length and formatting are ruff-enforced; run `.venv/bin/ruff format src tests` before each commit.
- No new third-party dependencies.
- Never add `--allow-empty`, `git push`, `git merge`, or any deploy verb to any git invocation. `src/dev_orchestration/git/guards.py` is the sole home of prohibited-verb literals, and `tests/git/test_guards.py` scans source for them by AST.
- Every task ends with a commit. Commit messages use the existing convention: `fix(area): imperative summary`, `feat(area): …`, `test(area): …`, `chore(area): …`.
- Do not weaken any assertion in `tests/git/test_guards.py`, `tests/adapters/test_registry.py`, or `tests/context/test_assembler.py::test_validation_receives_no_agent_output_whatsoever`. These encode controls the review found sound.

---

## File Structure

**Modified:**

- `src/dev_orchestration/git/repo.py` — gains `ignored_paths()`; `commit_snapshot` stops fabricating empty commits and stages only the reviewed set.
- `src/dev_orchestration/workflow/scope_check.py` — gains an ignored-write check alongside the existing fence check.
- `src/dev_orchestration/workflow/runner.py` — the terminal gate binds to the change inventory and the supplied criteria; tier downgrade refused; dead code removed.
- `src/dev_orchestration/config/resolver.py` — owns `ProtectedRuleViolation` and refuses an attempted override instead of normalising it.
- `src/dev_orchestration/workflow/bootstrap.py` — imports the exception rather than defining it; enforces the persistent-context budget.
- `src/dev_orchestration/workflow/tiers.py` — gains `TierDowngradeError`.
- `src/dev_orchestration/context/assembler.py` — enforces a prompt budget with explicit, recorded truncation; drops its dead `fence` parameter.
- `src/dev_orchestration/context/packet.py` — no change beyond what `Category` needs.
- `src/dev_orchestration/workflow/stages.py` — fingerprint-stable finding IDs; passes a durable schema directory.
- `src/dev_orchestration/workflow/invoke.py` — writes schemas into the run store, not a leaked temp directory.
- `src/dev_orchestration/artifacts/store.py` — persists the finding index.
- `src/dev_orchestration/config/models.py` — `RoleConfig` moves above `ProjectConfig`; `ProjectConfig` gains `roles`.
- `src/dev_orchestration/adapters/registry.py` — real framework < global < project precedence; `goal_executor` removed.
- `src/dev_orchestration/cli.py` — loads `GlobalConfig`; `--criterion`; new expected errors.
- `pyproject.toml` — Python floor.

**Test files touched:** `tests/git/test_repo.py`, `tests/workflow/test_scope_check.py`, `tests/workflow/test_runner.py`, `tests/workflow/test_invoke.py`, `tests/config/test_resolver.py`, `tests/context/test_assembler.py`, `tests/adapters/test_registry.py`, `tests/test_cli_run.py`, `tests/artifacts/test_store.py`, `tests/test_schemas.py`.

**Created:** `tests/test_packaging.py`.

---

## Task 0: Baseline the M2 implementation on a branch

Finding D1. Nothing else in this plan is reviewable until the code under review exists in git. Every subsequent task commits on top of this baseline, so the final evidence can name a real SHA.

**Files:**
- Modify: none (this task only commits existing working-tree state)

**Interfaces:**
- Consumes: nothing
- Produces: a branch `fix/m2-remediation` whose first commit contains the complete M2 implementation as reviewed

- [ ] **Step 1: Confirm the working tree is the reviewed state**

Run:
```bash
git status --porcelain | wc -l
.venv/bin/python -m pytest -q
```
Expected: 25 changed paths; `310 passed`.

If either differs, **stop and report** — the tree is not the state this plan was written against.

- [ ] **Step 2: Create the remediation branch**

```bash
git checkout -b fix/m2-remediation
```

- [ ] **Step 3: Commit the baseline**

```bash
git add -A
git commit -m "feat(m2): baseline the reviewed M2 loop implementation

The M2 loop was implemented but never committed; the acceptance evidence
in .superpowers/sdd/2026-08-29-m2-the-loop/validation/ names 9d20cf8, which
contains none of these files. This commit makes the reviewed state
reproducible so the remediation that follows has a real base."
```

- [ ] **Step 4: Verify the baseline is complete**

Run:
```bash
git status --porcelain
git ls-tree -r --name-only HEAD | grep -c "workflow/\|context/\|roles/"
```
Expected: empty status; a count greater than 20.

---

## Task 1: Give the Git inventory sight of ignored paths

Finding B3, part one. `git status --porcelain` omits ignored files and `--ignored=matching` collapses a wholly-ignored directory to a single `dist/` entry, so neither can tell the fence which files a run actually wrote. `git ls-files -o -i --exclude-standard` names every ignored file individually.

This task adds the primitive only. `change_inventory` deliberately does **not** change: folding ignored paths into it would flag `__pycache__` and `.pytest_cache` on every run. Attribution is Task 2's job.

**Files:**
- Modify: `src/dev_orchestration/git/repo.py`
- Test: `tests/git/test_repo.py`

**Interfaces:**
- Consumes: `GitRepo.run_git`
- Produces: `GitRepo.ignored_paths(self) -> list[str]` — sorted, repository-relative, individual file paths that git ignores

- [ ] **Step 1: Write the failing tests**

Append to `tests/git/test_repo.py`:

```python
def test_ignored_paths_names_individual_files_inside_an_ignored_directory(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist" / "sub").mkdir(parents=True)
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    (repo.root / "dist" / "sub" / "deep.txt").write_text("y\n")
    assert repo.ignored_paths() == ["dist/payload.sh", "dist/sub/deep.txt"]


def test_ignored_paths_excludes_tracked_and_plain_untracked_files(repo):
    (repo.root / ".gitignore").write_text("*.log\n")
    (repo.root / "app.log").write_text("noise\n")
    (repo.root / "visible.txt").write_text("seen\n")
    assert repo.ignored_paths() == ["app.log"]


def test_change_inventory_still_ignores_ignored_files(repo):
    base = repo.current_commit()
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    assert "dist/payload.sh" not in repo.change_inventory(base)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/git/test_repo.py -k ignored -v`
Expected: FAIL with `AttributeError: 'GitRepo' object has no attribute 'ignored_paths'`.

- [ ] **Step 3: Implement `ignored_paths`**

In `src/dev_orchestration/git/repo.py`, add this method to `GitRepo` immediately after `change_diff`:

```python
    def ignored_paths(self) -> list[str]:
        """Inventory every file git ignores, one path per file.

        `git status --porcelain` omits ignored files entirely, and
        `--ignored=matching` collapses a wholly-ignored tree to a single
        `dist/` entry. Neither can answer the only question the fence has:
        which files did this run write? `ls-files` names each one, so an
        agent cannot hide a change behind a .gitignore rule.
        """
        output = self.run_git("ls-files", "-o", "-i", "--exclude-standard", "-z", strip=False)
        return sorted({path for path in output.split("\0") if path})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/git/test_repo.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src/dev_orchestration/git/repo.py tests/git/test_repo.py
git commit -m "feat(git): inventory ignored files the change inventory cannot see"
```

---

## Task 2: Fence the ignored files a run actually writes

Finding B3, part two. Attribution is by snapshot delta around each agent stage: ignored paths present *before* an agent runs are pre-existing tool noise; paths that appear *across* an agent stage were written by that agent. Because validation runs between execution and remediation, its `__pycache__` output lands in the next snapshot's baseline and is never misattributed.

**Files:**
- Modify: `src/dev_orchestration/workflow/scope_check.py`, `src/dev_orchestration/workflow/runner.py:369-379`, `:435-444`
- Test: `tests/workflow/test_scope_check.py`, `tests/workflow/test_runner.py`

**Interfaces:**
- Consumes: `GitRepo.ignored_paths()` (Task 1), `ScopeFence.violations`, `ScopeViolation`
- Produces:
  - `check_ignored_writes(repo: GitRepo, before: list[str], fence: ScopeFence) -> list[str]`
  - `enforce_ignored_writes(repo: GitRepo, before: list[str], fence: ScopeFence) -> None`

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/workflow/test_scope_check.py` (add `check_ignored_writes` and `enforce_ignored_writes` to the existing import from `dev_orchestration.workflow.scope_check`):

```python
def test_ignored_file_written_outside_the_fence_is_a_violation(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    before = repo.ignored_paths()
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    assert check_ignored_writes(repo, before, FENCE) == ["dist/payload.sh"]


def test_ignored_file_present_before_the_stage_is_not_attributed_to_it(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "cache.bin").write_text("pre-existing\n")
    before = repo.ignored_paths()
    assert check_ignored_writes(repo, before, FENCE) == []


def test_ignored_file_written_inside_the_fence_is_allowed(repo):
    (repo.root / ".gitignore").write_text("*.log\n")
    before = repo.ignored_paths()
    (repo.root / "src" / "build.log").write_text("in scope\n")
    assert check_ignored_writes(repo, before, FENCE) == []


def test_enforcement_explains_why_an_ignored_write_is_invisible(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    before = repo.ignored_paths()
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    with pytest.raises(ScopeViolation) as error:
        enforce_ignored_writes(repo, before, FENCE)
    assert "dist/payload.sh" in str(error.value)
    assert "ignored" in str(error.value).lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/workflow/test_scope_check.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_ignored_writes'`.

- [ ] **Step 3: Implement the check**

Append to `src/dev_orchestration/workflow/scope_check.py`:

```python
def check_ignored_writes(repo: GitRepo, before: list[str], fence: ScopeFence) -> list[str]:
    """Return the ignored files this stage created that the fence disallows.

    Attribution is a delta, not an absolute: a repository legitimately
    carries ignored build and cache output, and validation adds more of it
    between stages. Only paths that appeared while an agent was running are
    that agent's doing.
    """
    created = sorted(set(repo.ignored_paths()) - set(before))
    return fence.violations(created)


def enforce_ignored_writes(repo: GitRepo, before: list[str], fence: ScopeFence) -> None:
    offenders = check_ignored_writes(repo, before, fence)
    if offenders:
        raise ScopeViolation(
            "run wrote git-ignored files outside the scope fence: "
            + ", ".join(offenders)
            + f". Fence allows {fence.include} and excludes {fence.exclude}. "
            "Ignored files never reach the diff the reviewer sees, so the fence "
            "is the only place this can be caught."
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/workflow/test_scope_check.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Write the failing integration test**

Append to `tests/workflow/test_runner.py`:

```python
def test_ignored_out_of_scope_write_escalates_the_run(tmp_path):
    git = repo(tmp_path)
    (git.root / ".gitignore").write_text(".ai/runs/\ndist/\n")
    subprocess.run(["git", "-C", str(git.root), "commit", "-qam", "ignore dist"], check=True)

    class Sneaky(Scripted):
        def run(self, request):
            if request.role == "implementation_worker":
                (request.cwd / "dist").mkdir(exist_ok=True)
                (request.cwd / "dist" / "payload.sh").write_text("#!/bin/sh\n")
            return super().run(request)

    outcome = execute_run(
        git, CONFIG, registry(Sneaky(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "dist/payload.sh" in outcome.reason
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_ignored_out_of_scope_write_escalates_the_run -v`
Expected: FAIL — `assert <RunState.COMPLETE_LOCAL> is <RunState.ESCALATED>`. This is the reproduction from finding B3.

- [ ] **Step 7: Wire the check into the runner**

In `src/dev_orchestration/workflow/runner.py`, extend the import from `dev_orchestration.workflow.scope_check`:

```python
from dev_orchestration.workflow.scope_check import ScopeViolation, enforce_fence, enforce_ignored_writes
```

Replace the execution block (currently lines 368-379, `engine.transition(RunState.EXECUTING)` through `enforce_fence(...)`) with:

```python
        engine.transition(RunState.EXECUTING)
        ignored_before = worktree_repo.ignored_paths()
        execute(
            registry,
            approved,
            invariants,
            runtime.references,
            worktree,
            store,
            runtime.profile_constraints,
            scope_context,
        )
        enforce_fence(worktree_repo, base_commit, fence)
        enforce_ignored_writes(worktree_repo, ignored_before, fence)
```

Then in the remediation loop, replace the `remediate(...)` call and the `enforce_fence` immediately after it (currently lines 434-444) with:

```python
            current_state = diff or "No committed or working-tree diff was observed."
            ignored_before = worktree_repo.ignored_paths()
            remediate(
                registry,
                blocking,
                approved,
                current_state,
                worktree,
                store,
                cycle=cycle,
            )
            enforce_fence(worktree_repo, base_commit, fence)
            enforce_ignored_writes(worktree_repo, ignored_before, fence)
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `312 passed`.

- [ ] **Step 9: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(scope): close the fence hole that let ignored paths through

change_inventory is git diff plus git status, and both omit ignored files,
so an agent could write dist/payload.sh in a repo that ignores dist/ and
the fence, the review diff, and the final commit would all report nothing.
Attribution is a snapshot delta around each agent stage, so pre-existing
build output and validation's own cache noise are never misattributed."
```

---

## Task 3: Refuse the empty commit and commit only the reviewed set

Findings B1 and E4. `commit_snapshot` discards its `paths` argument and passes `--allow-empty`, so a run that changed nothing fabricates a commit and certifies it. The fix is two-sided: the git layer refuses to record an empty snapshot, and the runner refuses to complete a run that produced no change.

This task also changes the shared test fakes. `Scripted` currently writes nothing, so seven tests assert that a no-op run completes — behaviour this task makes illegal. The fake becomes a worker that does its job, and a separate `NoOpWorker` covers the no-change case.

**Files:**
- Modify: `src/dev_orchestration/git/repo.py:137-142`, `src/dev_orchestration/workflow/runner.py:518-535`
- Test: `tests/git/test_repo.py`, `tests/workflow/test_runner.py`

**Interfaces:**
- Consumes: `GitRepo.change_inventory`
- Produces:
  - `EmptySnapshotError(RuntimeError)` in `dev_orchestration.git.repo`
  - `GitRepo.commit_snapshot(self, message: str, paths: list[str]) -> str` — raises `EmptySnapshotError` when `paths` is empty; stages and commits exactly `paths`

- [ ] **Step 1: Write the failing git-layer tests**

Append to `tests/git/test_repo.py` (add `EmptySnapshotError` to the existing import from `dev_orchestration.git.repo`):

```python
def test_commit_snapshot_refuses_an_empty_change_set(repo):
    with pytest.raises(EmptySnapshotError):
        repo.commit_snapshot("ai(run): nothing happened", [])


def test_commit_snapshot_records_only_the_reviewed_paths(repo):
    base = repo.current_commit()
    (repo.root / "reviewed.txt").write_text("in the review\n")
    (repo.root / "unreviewed.txt").write_text("not in the review\n")
    sha = repo.commit_snapshot("ai(run): complete", ["reviewed.txt"])
    committed = repo.run_git("diff", "--name-only", base, sha).splitlines()
    assert committed == ["reviewed.txt"]


def test_commit_snapshot_records_a_deletion(repo):
    base = repo.current_commit()
    (repo.root / "README.md").unlink()
    sha = repo.commit_snapshot("ai(run): remove", ["README.md"])
    assert repo.run_git("diff", "--name-status", base, sha) == "D\tREADME.md"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/git/test_repo.py -k commit_snapshot -v`
Expected: FAIL with `ImportError: cannot import name 'EmptySnapshotError'`.

- [ ] **Step 3: Implement the refusal**

In `src/dev_orchestration/git/repo.py`, add the exception next to `DirtyWorktreeError`:

```python
class EmptySnapshotError(RuntimeError):
    """A run reached its final commit with no change to record."""
```

Replace `commit_snapshot` entirely:

```python
    def commit_snapshot(self, message: str, paths: list[str]) -> str:
        """Commit exactly the reviewed change set. Never empty, never wider.

        The previous implementation discarded `paths`, staged with `add -A`,
        and passed `--allow-empty`, which meant a run where the agent did
        nothing still produced a commit the manifest recorded as the run's
        final code state. An empty commit is not evidence of work, and a
        commit wider than the reviewed inventory is not the thing that was
        reviewed.
        """
        if not paths:
            raise EmptySnapshotError(
                f"{self.root} has no change to commit; a run must not record an empty commit"
            )
        self.run_git("add", "--", *paths)
        self.run_git("commit", "-q", "-m", message, "--", *paths)
        return self.current_commit()
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/git/test_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Update the shared test fakes and add the no-op test**

In `tests/workflow/test_runner.py`, replace the `Scripted.run` method so the default worker does its job, and replace the `WritingWorker` class with a `NoOpWorker`.

Change `Scripted.run` (currently lines 51-57) to:

```python
    def run(self, request):
        self.seen.append(request.role)
        self.requests.append(request)
        if request.role == "implementation_worker":
            (request.cwd / "src" / "app.py").write_text("x = 2\n")
        values = self.script[request.role]
        payload = values.pop(0) if len(values) > 1 else values[0]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, payload, now, now)
```

Delete the `WritingWorker` class (lines 90-94) and add in its place:

```python
class NoOpWorker(Scripted):
    """A worker that returns success without changing anything."""

    def run(self, request):
        if request.role == "implementation_worker":
            self.seen.append(request.role)
            self.requests.append(request)
            now = datetime.now(UTC)
            return AgentResult("fake", None, 0, self.script[request.role][0], now, now)
        return super().run(request)
```

In `test_complete_local_persists_clean_final_commit`, replace `registry(WritingWorker(script()))` with `registry(Scripted(script()))`.

Replace `test_standard_run_reaches_complete_local_and_persists_final_commit` (lines 132-141) with:

```python
def test_run_that_changes_nothing_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git, CONFIG, registry(NoOpWorker(script())), "add a flag", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.FAILED
    assert "no change" in outcome.reason
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert manifest["git"]["final_commit"] is None
```

- [ ] **Step 6: Run to verify the no-op test fails**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_run_that_changes_nothing_cannot_complete_local -v`
Expected: FAIL — `assert <RunState.COMPLETE_LOCAL> is <RunState.FAILED>`. This is the reproduction from finding B1.

- [ ] **Step 7: Gate the runner on a real change**

In `src/dev_orchestration/workflow/runner.py`, extend the import from `dev_orchestration.git.repo`:

```python
from dev_orchestration.git.repo import EmptySnapshotError, GitRepo
```

Replace the final-commit block (currently lines 518-535, from `enforce_fence(...)` through the `store.update_manifest(git=GitBlock(...))` call) with:

```python
        enforce_fence(worktree_repo, base_commit, fence)
        changed = worktree_repo.change_inventory(base_commit)
        final_commit = worktree_repo.current_commit()
        if not changed and final_commit == base_commit:
            _terminal(engine, RunState.FAILED, "the run produced no change to commit")
            return _outcome(engine, verification)
        if changed:
            final_commit = worktree_repo.commit_snapshot(
                f"ai({store.run_id}): complete orchestrated run", changed
            )
        worktree_repo.ensure_clean()
        if final_commit == base_commit:
            raise ContextContractError("final commit does not advance from the recorded base")
        if not worktree_repo.is_ancestor(base_commit, final_commit):
            raise ContextContractError("final commit does not descend from the recorded base")
        store.update_manifest(
            git=GitBlock(
                base_commit=base_commit,
                branch=store.read_manifest().git.branch,
                worktree=str(worktree),
                final_commit=final_commit,
            )
        )
```

Add `EmptySnapshotError` to the first `except` tuple (currently lines 543-550), after `ScopeViolation`:

```python
    except (
        RemediationExhausted,
        ScopeViolation,
        EmptySnapshotError,
        SchemaEscalation,
        AgentInvocationError,
        ContextContractError,
        ReadOnlyRoleUnsupportedError,
    ) as exc:
```

- [ ] **Step 8: Add `EmptySnapshotError` to the CLI's expected errors**

In `src/dev_orchestration/cli.py`, change the import:

```python
from dev_orchestration.git.repo import (
    DirtyWorktreeError,
    EmptySnapshotError,
    GitCommandError,
    discover_repo,
)
```

and add `EmptySnapshotError,` to the `EXPECTED_ERRORS` tuple after `DirtyWorktreeError,`.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `312 passed`.

- [ ] **Step 10: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(runner): a run that changed nothing cannot complete

commit_snapshot discarded its paths argument, staged with add -A, and
committed --allow-empty, so a worker that did nothing produced a commit
the manifest recorded as the run's final code state -- an empty commit
certified as a completed run. The snapshot now stages exactly the
inventory the reviewer and verifier saw, and a run with no change fails."
```

---

## Task 4: Bind final verification to the run's own acceptance criteria

Finding B2. `Verification.every_criterion_has_a_verdict` only checks the provider's payload against itself, so `criteria: []` with `verdicts: []` and `outcome: PASS` validates, and `all([])` is `True`.

**Files:**
- Modify: `src/dev_orchestration/workflow/runner.py:181-213`, `:506-516`
- Test: `tests/workflow/test_runner.py`

**Interfaces:**
- Consumes: `Verification.criteria`, the `acceptance_criteria` argument to `execute_run`
- Produces: no new symbols; `execute_run` now raises `ContextContractError` when called with no criteria

- [ ] **Step 1: Write the failing tests**

Append to `tests/workflow/test_runner.py`:

```python
def test_verifier_that_judges_no_criteria_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    empty = {"outcome": "PASS", "criteria": [], "verdicts": [], "unresolved_finding_ids": []}
    outcome = execute_run(
        git, CONFIG, registry(Scripted(script(verifier=empty))), "do it", tmp_path / "wt", CRITERIA
    )
    assert outcome.final_state is RunState.FAILED
    assert "acceptance criteria" in outcome.reason


def test_verifier_that_swaps_in_its_own_criteria_cannot_complete_local(tmp_path):
    git = repo(tmp_path)
    swapped = {
        "outcome": "PASS",
        "criteria": ["something easier"],
        "verdicts": [{"criterion": "something easier", "verdict": "PASS", "evidence": "sure"}],
        "unresolved_finding_ids": [],
    }
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(verifier=swapped))),
        "do it",
        tmp_path / "wt",
        CRITERIA,
    )
    assert outcome.final_state is RunState.FAILED


def test_a_run_without_acceptance_criteria_is_refused(tmp_path):
    git = repo(tmp_path)
    with pytest.raises(ContextContractError):
        execute_run(git, CONFIG, registry(Scripted(script())), "do it", tmp_path / "wt", [])
```

Add the import at the top of the file:

```python
from dev_orchestration.context.assembler import ContextContractError
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py -k "no_criteria or swaps_in or without_acceptance" -v`
Expected: the first two FAIL with `assert <RunState.COMPLETE_LOCAL> is <RunState.FAILED>` (the reproduction from finding B2); the third FAILS with `DID NOT RAISE`.

- [ ] **Step 3: Refuse a run with no criteria**

In `src/dev_orchestration/workflow/runner.py`, insert at the very top of `execute_run`'s body, before the `tier_override` check:

```python
    if not acceptance_criteria:
        raise ContextContractError(
            "a run requires at least one acceptance criterion; "
            "there is nothing for final verification to judge"
        )
```

- [ ] **Step 4: Bind the verification to those criteria**

Replace the verification gate (currently lines 508-516) with:

```python
        if list(verification.criteria) != list(acceptance_criteria):
            _terminal(
                engine,
                RunState.FAILED,
                "final verification did not judge the supplied acceptance criteria",
            )
            return _outcome(engine, verification)
        all_verdicts_pass = all(verdict.verdict == "PASS" for verdict in verification.verdicts)
        if (
            verification.outcome is not Outcome.PASS
            or not all_verdicts_pass
            or set(verification.unresolved_finding_ids) & set(implementation_review.blocking_ids())
        ):
            _terminal(engine, RunState.FAILED, "final verification was not an unqualified PASS")
            return _outcome(engine, verification)
```

Note the trailing `or implementation_review.blocking_ids()` is dropped: the loop above cannot exit with blocking findings, so it was always false.

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `315 passed`.

- [ ] **Step 7: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(runner): verification must judge the run's own criteria

Verification.every_criterion_has_a_verdict only checked the provider's
payload against itself, so a verifier returning criteria: [] with
verdicts: [] and outcome: PASS validated and all([]) certified the run.
The gate now compares the returned criteria to the ones the run supplied."
```

---

## Task 5: Raise the Python floor to the version the fence needs

Finding B4. `scope.py:42` calls `PurePosixPath.full_match`, added in CPython 3.13, while `pyproject.toml` declares `>=3.11`. On 3.11 or 3.12 any fence pattern containing `*`, `?`, or `[` raises `AttributeError` inside the safety control.

**Files:**
- Modify: `pyproject.toml:5`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing importable; a regression guard on the declared floor

- [ ] **Step 1: Write the failing test**

Create `tests/test_packaging.py`:

```python
import re
import sys
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# scope._matches calls PurePosixPath.full_match, added in CPython 3.13. On an
# older interpreter the scope fence -- the control that decides what a run may
# modify -- raises AttributeError on any glob pattern. The declared floor must
# not promise an interpreter the fence cannot run on.
FENCE_API_FLOOR = (3, 13)


def test_declared_python_floor_supports_the_fence_glob_api():
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', PYPROJECT.read_text())
    assert match, "pyproject.toml must declare requires-python as >=MAJOR.MINOR"
    assert (int(match.group(1)), int(match.group(2))) >= FENCE_API_FLOOR


def test_the_running_interpreter_provides_the_fence_glob_api():
    from pathlib import PurePosixPath

    assert hasattr(PurePosixPath("a"), "full_match")
    assert sys.version_info[:2] >= FENCE_API_FLOOR


def test_fence_glob_patterns_work_on_this_interpreter():
    from dev_orchestration.scope import ScopeFence

    fence = ScopeFence(include=["src/*.py"], exclude=[])
    assert fence.allows("src/app.py")
    assert not fence.allows("src/nested/app.py")
```

- [ ] **Step 2: Run to verify the floor test fails**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -v`
Expected: `test_declared_python_floor_supports_the_fence_glob_api` FAILS (`(3, 11) >= (3, 13)` is false); the other two PASS.

- [ ] **Step 3: Raise the floor**

In `pyproject.toml`, change line 5:

```toml
requires-python = ">=3.13"
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add pyproject.toml tests/test_packaging.py
git commit -m "fix(packaging): declare the Python floor the scope fence needs

scope._matches uses PurePosixPath.full_match, which is 3.13+, while the
package advertised >=3.11. On 3.11 or 3.12 every glob fence pattern
raised AttributeError inside the control that decides what a run may
modify -- invisible here because the local interpreter is 3.14."
```

---

## Task 6: Refuse a protected-rule override instead of normalising it

Finding D4. `resolve_policy` ends with an unconditional `resolved.update(PROTECTED_DEFAULTS)`, so a layer setting `protected.autonomous_push: true` is silently rewritten to `False` and `ProtectedRuleViolation` is unreachable.

**Files:**
- Modify: `src/dev_orchestration/config/resolver.py`, `src/dev_orchestration/workflow/bootstrap.py:19-21`, `:10`, `src/dev_orchestration/workflow/runner.py:543-550`
- Test: `tests/config/test_resolver.py`

**Interfaces:**
- Consumes: `PROTECTED_DEFAULTS`
- Produces: `ProtectedRuleViolation(RuntimeError)` now defined in `dev_orchestration.config.resolver` and re-exported by `dev_orchestration.workflow.bootstrap` (existing importers keep working)

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_resolver.py`:

```python
def test_a_layer_that_sets_a_protected_rule_is_refused():
    with pytest.raises(ProtectedRuleViolation) as error:
        resolve_policy([{"protected": {"autonomous_push": True}}])
    assert "autonomous_push" in str(error.value)


def test_a_layer_that_restates_a_protected_rule_as_false_is_accepted():
    resolved = resolve_policy([{"protected": {"autonomous_push": False}}])
    assert resolved["protected.autonomous_push"] is False


def test_a_later_layer_cannot_reopen_a_protected_rule():
    with pytest.raises(ProtectedRuleViolation):
        resolve_policy([{"protected": {"autonomous_deploy": False}}, {"protected": {"autonomous_deploy": True}}])
```

Add to the file's imports:

```python
import pytest

from dev_orchestration.config.resolver import ProtectedRuleViolation, resolve_policy
```

(keep whatever `resolve_policy` import already exists; do not duplicate it)

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/config/test_resolver.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProtectedRuleViolation'`.

- [ ] **Step 3: Move the exception and refuse the override**

In `src/dev_orchestration/config/resolver.py`, add after `PROTECTED_DEFAULTS`:

```python
class ProtectedRuleViolation(RuntimeError):
    """A configuration layer tried to set a protected rule."""
```

Replace `resolve_policy` with:

```python
def resolve_policy(layers: list[dict]) -> dict[str, Any]:
    """Merge configuration layers into one flat dotted-key policy.

    A protected rule is refused, not overwritten. Silently rewriting an
    attempted override to False left the rule correct but the operator
    uninformed: the run proceeded with no event, no message, and no
    escalation, and the guard that was supposed to catch it could never
    fire because this function had already erased the evidence.
    """
    resolved: dict[str, Any] = {}
    for layer in layers:
        for key, value in _flatten(layer).items():
            if key in PROTECTED_DEFAULTS and value is not False:
                raise ProtectedRuleViolation(
                    f"{key} was set to {value!r}; protected rules are not overridable. "
                    "Remove the setting; dev-orch will not run against a configuration "
                    "that asks for it."
                )
            if _is_strictness_key(key) and key in resolved:
                resolved[key] = bool(resolved[key]) or bool(value)
            else:
                resolved[key] = value
    resolved.update(PROTECTED_DEFAULTS)
    return resolved
```

- [ ] **Step 4: Re-point bootstrap at the moved exception**

In `src/dev_orchestration/workflow/bootstrap.py`, change the resolver import to:

```python
from dev_orchestration.config.resolver import (
    PROTECTED_DEFAULTS,
    ProtectedRuleViolation,
    resolve_policy,
)
```

and delete the local class definition (lines 19-21). The post-check at `bootstrap_run` stays as defence in depth.

- [ ] **Step 5: Let the runner escalate on it**

In `src/dev_orchestration/workflow/runner.py`, add to the import from `dev_orchestration.config.resolver`:

```python
from dev_orchestration.config.resolver import PROTECTED_DEFAULTS, ProtectedRuleViolation
```

and add `ProtectedRuleViolation,` to the first `except` tuple, after `ScopeViolation,`. Without this the exception falls through to the broad handler and is reported as `FAILED: unexpected workflow failure` rather than a safety escalation.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `318 passed`.

- [ ] **Step 7: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(config): refuse a protected-rule override, do not normalise it

resolve_policy ended with an unconditional update(PROTECTED_DEFAULTS), so
a config setting protected.autonomous_push: true resolved to False with no
refusal and no event, and the ProtectedRuleViolation guards in bootstrap
and the runner were unreachable."
```

---

## Task 7: A tier override may raise the tier but never lower it

Finding D6. `--tier trivial` against a `standard` classification skips planning and plan review entirely.

**Files:**
- Modify: `src/dev_orchestration/workflow/tiers.py`, `src/dev_orchestration/workflow/runner.py:170-178`, `:543-555`, `src/dev_orchestration/cli.py`
- Test: `tests/workflow/test_tiers.py`, `tests/test_cli_run.py:196-241`

**Interfaces:**
- Consumes: `Tier`, `_tier_rank`
- Produces: `TierDowngradeError(RuntimeError)` in `dev_orchestration.workflow.tiers`

- [ ] **Step 1: Write the failing runner tests**

Append to `tests/workflow/test_runner.py`:

```python
def test_override_may_raise_the_tier(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="trivial"))),
        "fix",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.STANDARD,
    )
    manifest = json.loads((outcome.store_root / "manifest.json").read_text())
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert manifest["tier"] == "standard"


def test_override_may_not_lower_the_tier_below_the_classification(tmp_path):
    git = repo(tmp_path)
    outcome = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(tier="standard"))),
        "rewrite auth",
        tmp_path / "wt",
        CRITERIA,
        tier_override=Tier.TRIVIAL,
    )
    assert outcome.final_state is RunState.ESCALATED
    assert "trivial" in outcome.reason and "standard" in outcome.reason
```

Add `Tier` to the existing import from `dev_orchestration.domain.enums` at the top of the file.

- [ ] **Step 2: Run to verify the downgrade test fails**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py -k "may_raise or may_not_lower" -v`
Expected: `test_override_may_not_lower_the_tier_below_the_classification` FAILS — `assert <RunState.COMPLETE_LOCAL> is <RunState.ESCALATED>`.

- [ ] **Step 3: Add the exception**

In `src/dev_orchestration/workflow/tiers.py`, add after `UnsupportedTierError`:

```python
class TierDowngradeError(RuntimeError):
    """An override tried to run under weaker controls than the classification."""
```

- [ ] **Step 4: Enforce it**

In `src/dev_orchestration/workflow/runner.py`, extend the tiers import:

```python
from dev_orchestration.workflow.tiers import TierDowngradeError, UnsupportedTierError, stages_for
```

Replace `_effective_tier` (lines 170-178) with:

```python
def _effective_tier(
    classification_tier: Tier,
    override: Tier | None,
    minimum: Tier | None,
) -> Tier:
    """Resolve the tier a run executes under. Overrides raise; they never lower.

    A tier is a set of controls, not a preference. Letting `--tier trivial`
    win over a `standard` classification skipped planning and plan review
    on exactly the runs that most needed them.
    """
    requested = classification_tier
    if override is not None:
        if _tier_rank(override) < _tier_rank(classification_tier):
            raise TierDowngradeError(
                f"--tier {override} is below the classified tier {classification_tier}; "
                "an override may raise a run's controls but never lower them"
            )
        requested = override
    if minimum and _tier_rank(requested) < _tier_rank(minimum):
        requested = minimum
    return requested
```

Add `TierDowngradeError` to the runner's second `except` clause:

```python
    except (IllegalTransitionError, UnsupportedTierError, TierDowngradeError) as exc:
```

- [ ] **Step 5: Add it to the CLI's expected errors**

In `src/dev_orchestration/cli.py`, change the tiers import to:

```python
from dev_orchestration.workflow.tiers import TierDowngradeError, UnsupportedTierError
```

and add `TierDowngradeError,` to `EXPECTED_ERRORS` after `UnsupportedTierError,`.

- [ ] **Step 6: Fix the CLI test that asserted the downgrade**

In `tests/test_cli_run.py`, replace the accepted-override block of `test_tier_override_is_effective_persisted_and_cannot_beat_minimum` (lines 197-208) with an upgrade plus an explicit rejection, so the test finally covers A9's "accepted **and rejected** override cases":

```python
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    repo(accepted_root)
    accepted_adapter = ScriptedAdapter(tier="trivial")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(accepted_adapter))
    _use_local_worktree(monkeypatch, accepted_root)
    monkeypatch.chdir(accepted_root)
    accepted = CliRunner().invoke(app, ["run", "fix", "--tier", "standard"])
    assert accepted.exit_code == 0, accepted.output
    accepted_manifest = _latest_manifest(accepted_root)
    assert accepted_manifest.tier is Tier.STANDARD
    assert accepted_manifest.tier_override is Tier.STANDARD

    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    repo(rejected_root)
    rejected_adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(rejected_adapter))
    _use_local_worktree(monkeypatch, rejected_root)
    monkeypatch.chdir(rejected_root)
    rejected = CliRunner().invoke(app, ["run", "rewrite auth", "--tier", "trivial"])
    assert rejected.exit_code == 1
    assert "Traceback" not in rejected.output
    assert _latest_manifest(rejected_root).status is RunState.ESCALATED
```

In the same file, change the two remaining `lambda config: registry(...)` monkeypatches (in the minimum block and in `test_external_plan_is_imported_hashed_reviewed_and_not_regenerated`) to `lambda *a, **k: registry(...)`, so they tolerate the extra argument `default_registry` receives after Task 9.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `320 passed`.

- [ ] **Step 8: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(tiers): an override may raise the tier, never lower it

--tier trivial against a standard classification skipped planning and
plan review outright, and the CLI test asserted that downgrade was
correct. A tier is a set of controls, not a preference."
```

---

## Task 8: Let a caller state real acceptance criteria

Finding D7. `run` hardcodes `acceptance_criteria=[request]`, so every run asks the verifier "did you do &lt;the request text&gt;?" as its single criterion.

**Files:**
- Modify: `src/dev_orchestration/cli.py:112-148`
- Test: `tests/test_cli_run.py`

**Interfaces:**
- Consumes: `execute_run(..., acceptance_criteria=...)`
- Produces: `dev-orch run --criterion TEXT` (repeatable)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_run.py`:

```python
def test_criteria_are_taken_from_the_command_line_when_supplied(tmp_path, monkeypatch):
    root = tmp_path / "criteria"
    root.mkdir()
    repo(root)
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(ScriptedAdapter()))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app,
        ["run", "add a flag", "--criterion", "the flag exists", "--criterion", "tests cover it"],
    )
    assert result.exit_code == 0, result.output
    assert _latest_manifest(root).acceptance_criteria == ["the flag exists", "tests cover it"]


def test_criteria_fall_back_to_the_request_and_say_so(tmp_path, monkeypatch):
    root = tmp_path / "fallback"
    root.mkdir()
    repo(root)
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(ScriptedAdapter()))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag"])
    assert result.exit_code == 0, result.output
    assert _latest_manifest(root).acceptance_criteria == ["add a flag"]
    assert "--criterion" in result.output
```

`ScriptedAdapter.run` must satisfy whatever criteria the CLI passes, so its verifier payload can no longer be a fixed constant. Delete the module-level `VERIFY` constant (nothing else references it) and replace `ScriptedAdapter.run` so the fake echoes back the criteria it was actually handed:

The `assert "--criterion" in result.output` below relies on `CliRunner` folding stderr into `result.output`, which this suite already depends on — `test_run_refuses_dirty_repository_and_names_file` asserts on a message `cli.py` writes with `err=True`.

```python
    def run(self, request):
        self.seen.append(request.role)
        if request.role == "implementation_worker":
            (request.cwd / "src" / "app.py").write_text("x = 2\n")
        if request.role == "verifier":
            criteria = _criteria_from(request)
            payload = {
                "outcome": "PASS",
                "criteria": criteria,
                "verdicts": [
                    {"criterion": item, "verdict": "PASS", "evidence": "seen"} for item in criteria
                ],
                "unresolved_finding_ids": [],
            }
        else:
            payload = {
                "classifier": {"tier": self.tier, "rationale": "test", "profiles": self.profiles},
                "planner": PLAN,
                "plan_reviewer": PASS,
                "plan_reconciler": PLAN,
                "implementation_worker": "done",
                "implementation_reviewer": PASS,
            }[request.role]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, payload, now, now)
```

and add the helper above the class:

```python
def _criteria_from(request):
    """Read the acceptance criteria out of the packet the verifier was handed."""
    for item in request.context.items:
        if item.label == "acceptance_criteria":
            return [line for line in item.content.splitlines() if line]
    return []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_run.py -k criteria -v`
Expected: FAIL — `Error: No such option: --criterion`.

- [ ] **Step 3: Add the option**

In `src/dev_orchestration/cli.py`, change the `run` signature and criteria resolution:

```python
@app.command()
def run(
    request: str = typer.Argument(..., help="What you want done"),
    plan_file: Path | None = typer.Option(  # noqa: B008
        None, "--plan", help="Import an external plan"
    ),
    tier: str | None = typer.Option(None, "--tier", help="Raise the classified tier"),
    criterion: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--criterion",
        help="An acceptance criterion final verification must judge; repeat for several",
    ),
) -> None:
    """Execute one local run. It never pushes, merges, or deploys."""
    criteria = list(criterion or [])
    if not criteria:
        criteria = [request]
        typer.echo(
            "No --criterion given; verifying against the request text itself. "
            "Pass --criterion to state what done actually means.",
            err=True,
        )
```

and change the `execute_run` call's `acceptance_criteria=[request]` to `acceptance_criteria=criteria`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_run.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `322 passed`.

- [ ] **Step 6: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "feat(cli): let a run state its acceptance criteria

run() hardcoded acceptance_criteria=[request], so every run asked the
verifier whether the request text had happened, with no way to say what
done actually means. Falling back to the request is still allowed, but
it now says so on stderr rather than pretending it is a criterion."
```

---

## Task 9: Resolve role bindings through project and global configuration

Finding D3. `default_registry` does `del project_config`, `GlobalConfig` is never loaded from disk, and `ProjectConfig` has no `roles` field, so role bindings, model aliases, `codex_binary`, and `worktree_root` are all unreachable configuration.

**Files:**
- Modify: `src/dev_orchestration/config/models.py:89-109`, `src/dev_orchestration/adapters/registry.py:94-110`, `src/dev_orchestration/cli.py`
- Test: `tests/adapters/test_registry.py`, `tests/config/test_models.py`, `tests/test_cli_run.py`

**Interfaces:**
- Consumes: `DEFAULT_ROLES`, `GlobalConfig`, `ProjectConfig`
- Produces:
  - `ProjectConfig.roles: dict[str, RoleConfig]`
  - `default_registry(project_config: ProjectConfig | None = None, global_config: GlobalConfig | None = None) -> RoleRegistry` with framework &lt; global &lt; project precedence
  - `cli.GLOBAL_CONFIG_PATH: Path` and `cli._global_config(path: Path | None = None) -> GlobalConfig`

- [ ] **Step 1: Write the failing tests**

Append to `tests/adapters/test_registry.py`:

```python
def test_project_roles_override_global_which_override_framework_defaults():
    from dev_orchestration.adapters.registry import default_registry
    from dev_orchestration.config.models import GlobalConfig, ProjectConfig, RoleConfig

    project = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "class": "internal_utility"},
            "roles": {"planner": {"adapter": "claude", "model": "opus"}},
        }
    )
    global_config = GlobalConfig(
        roles={
            "planner": RoleConfig(adapter="codex", model="luna"),
            "classifier": RoleConfig(adapter="claude", model="opus"),
        }
    )
    registry = default_registry(project, global_config)
    assert registry.binding_for("planner").adapter == "claude"
    assert registry.binding_for("planner").model == "opus"
    assert registry.binding_for("classifier").adapter == "claude"
    assert registry.binding_for("plan_reconciler").adapter == "codex"


def test_default_registry_without_configuration_uses_framework_defaults():
    from dev_orchestration.adapters.registry import DEFAULT_ROLES, default_registry

    registry = default_registry()
    assert registry.roles == DEFAULT_ROLES
```

Append to `tests/test_cli_run.py`:

```python
def test_global_config_supplies_the_worktree_root_and_role_bindings(tmp_path, monkeypatch):
    global_path = tmp_path / "config.yaml"
    global_path.write_text(
        "worktree_root: "
        + str(tmp_path / "elsewhere")
        + "\nroles:\n  planner:\n    adapter: claude\n    model: opus\n"
    )
    monkeypatch.setattr(cli_module, "GLOBAL_CONFIG_PATH", global_path)
    loaded = cli_module._global_config()
    assert loaded.worktree_root == str(tmp_path / "elsewhere")
    assert loaded.roles["planner"].adapter == "claude"


def test_missing_global_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "GLOBAL_CONFIG_PATH", tmp_path / "absent.yaml")
    assert cli_module._global_config().worktree_root == "~/.dev-orchestration/worktrees"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/adapters/test_registry.py tests/test_cli_run.py -k "override or framework_defaults or global_config" -v`
Expected: FAIL — pydantic rejects the unknown `roles` key on `ProjectConfig`, and `cli_module` has no `_global_config`.

- [ ] **Step 3: Give `ProjectConfig` a roles field**

In `src/dev_orchestration/config/models.py`, move the `RoleConfig` class definition (currently lines 99-102) to sit immediately **above** `class ProjectConfig`, then add the field to `ProjectConfig`:

```python
class ProjectConfig(BaseModel):
    schema_version: str = "1.0"
    project: ProjectMeta
    profiles: Profiles = Field(default_factory=Profiles)
    scope: Scope = Field(default_factory=Scope)
    validation: dict[str, ValidationCommand] = Field(default_factory=dict)
    git: GitPolicy = Field(default_factory=GitPolicy)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
```

- [ ] **Step 4: Make `default_registry` honour both layers**

In `src/dev_orchestration/adapters/registry.py`, replace `default_registry`:

```python
def default_registry(
    project_config: ProjectConfig | None = None,
    global_config: GlobalConfig | None = None,
) -> RoleRegistry:
    """Bind roles with framework < global < project precedence.

    This previously did `del project_config`, which made every repository's
    role, adapter, and model configuration inert: the framework defaults
    were the only bindings that could ever apply.
    """
    global_config = global_config or GlobalConfig()
    roles = dict(DEFAULT_ROLES)
    roles.update(global_config.roles)
    if project_config is not None:
        roles.update(project_config.roles)
    codex_override = (
        Path(global_config.codex_binary).expanduser() if global_config.codex_binary else None
    )
    adapters: dict[str, AgentAdapter] = {
        "codex": CodexAdapter(binary=discover_codex(codex_override)),
        "claude": ClaudeAdapter(),
    }
    return RoleRegistry(roles=roles, adapters=adapters)
```

- [ ] **Step 5: Load the global config in the CLI**

In `src/dev_orchestration/cli.py`, add `GlobalConfig` to the config import, then add below `_project_config`:

```python
GLOBAL_CONFIG_PATH = Path.home() / ".dev-orchestration" / "config.yaml"


def _global_config(path: Path | None = None) -> GlobalConfig:
    """Load machine-level configuration, or its defaults when absent."""
    target = path or GLOBAL_CONFIG_PATH
    if not target.is_file():
        return GlobalConfig()
    return GlobalConfig.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")) or {})
```

In `run`, replace the registry construction and the hardcoded worktree root:

```python
        global_config = _global_config()
        registry = default_registry(config, global_config)
        outcome = execute_run(
            repo,
            config,
            registry,
            request,
            Path(global_config.worktree_root).expanduser(),
            acceptance_criteria=criteria,
            tier_override=override,
            external_plan=plan_file,
        )
```

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters/test_registry.py tests/test_cli_run.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `326 passed`.

- [ ] **Step 8: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(config): wire the role bindings that were being discarded

default_registry took project_config and immediately deleted it, and
GlobalConfig was never read from disk, so role bindings, model aliases,
codex_binary, and worktree_root were unreachable configuration and the
framework defaults were the only bindings that could ever apply."
```

---

## Task 10: Enforce a prompt budget with explicit, recorded truncation

Finding D2. `compose_prompt` puts the whole context packet — including the full `git diff` — into one argv element. Linux caps a single argument at `MAX_ARG_STRLEN` (128 KiB), so a real diff fails with `E2BIG`. The budget is deliberately below that ceiling to leave room for the rest of the command line.

Plans, findings, and criteria are never truncated: if they alone exceed the budget the run escalates rather than sending a silently incomplete contract.

**Files:**
- Modify: `src/dev_orchestration/context/assembler.py`, `src/dev_orchestration/workflow/bootstrap.py:73-87`
- Test: `tests/context/test_assembler.py`

**Interfaces:**
- Consumes: `ContextRef`, `ContextPacket`, `Category`
- Produces:
  - `PROMPT_BUDGET_BYTES: int` and `TRUNCATABLE: frozenset[Category]` in `dev_orchestration.context.assembler`
  - `Category.CONTEXT_NOTES`
  - `assemble(stage: str, offered: list[ContextRef]) -> ContextPacket` — the dead `fence` parameter is removed (finding E1)

- [ ] **Step 1: Write the failing tests**

Append to `tests/context/test_assembler.py`:

```python
def test_an_oversized_diff_is_truncated_and_the_packet_says_so():
    packet = assemble(
        "implementation_review",
        [
            ref(Category.APPROVED_PLAN, "the plan"),
            ref(Category.DIFF, "x" * (PROMPT_BUDGET_BYTES + 50_000)),
            ref(Category.VALIDATION_EVIDENCE, "unit: exit 0"),
        ],
    )
    assert packet.total_bytes() <= PROMPT_BUDGET_BYTES
    assert Category.CONTEXT_NOTES in packet.labels()
    assert "truncated" in packet.render()
    assert "the plan" in packet.render()


def test_a_packet_within_budget_is_untouched():
    packet = assemble(
        "implementation_review",
        [ref(Category.APPROVED_PLAN, "the plan"), ref(Category.DIFF, "small diff")],
    )
    assert Category.CONTEXT_NOTES not in packet.labels()
    assert "small diff" in packet.render()


def test_oversized_non_truncatable_context_fails_closed():
    with pytest.raises(ContextContractError) as error:
        assemble(
            "implementation_review",
            [ref(Category.APPROVED_PLAN, "p" * (PROMPT_BUDGET_BYTES + 1))],
        )
    assert "budget" in str(error.value)
```

Add `PROMPT_BUDGET_BYTES` to the existing import from `dev_orchestration.context.assembler`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/context/test_assembler.py -k budget -v`
Expected: FAIL with `ImportError: cannot import name 'PROMPT_BUDGET_BYTES'`.

- [ ] **Step 3: Implement the budget**

In `src/dev_orchestration/context/assembler.py`, add `CONTEXT_NOTES = "context_notes"` to the `Category` enum, then add above `assemble`:

```python
# A composed prompt is passed to the provider as a single argv element, and
# Linux caps one argument at MAX_ARG_STRLEN (128 KiB). Without a budget a
# real diff simply fails the run with E2BIG, and nothing in a fake-adapter
# test can see it. The margin below that ceiling is for the rest of argv.
PROMPT_BUDGET_BYTES = 96_000
TRUNCATION_MARKER = "\n\n[... truncated to fit the prompt budget ...]"

# Evidence may be shortened; a contract may not. If the plan, the findings,
# or the criteria alone exceed the budget the run escalates rather than
# sending an agent a contract with a piece silently missing.
TRUNCATABLE: frozenset[Category] = frozenset(
    {
        Category.DIFF,
        Category.CURRENT_STATE,
        Category.VALIDATION_EVIDENCE,
        Category.REFERENCES,
    }
)


def _size(item: ContextRef) -> int:
    return len(item.content.encode("utf-8"))


def _fit_to_budget(items: list[ContextRef], budget: int) -> tuple[list[ContextRef], list[str]]:
    """Shrink truncatable items until the packet fits. Never drop a fixed one."""
    fixed = sum(_size(item) for item in items if Category(item.label) not in TRUNCATABLE)
    if fixed > budget:
        raise ContextContractError(
            f"non-truncatable context is {fixed} bytes, over the {budget}-byte prompt budget; "
            "this request cannot be sent without dropping part of the contract"
        )
    if sum(_size(item) for item in items) <= budget:
        return items, []
    truncatable = [item for item in items if Category(item.label) in TRUNCATABLE]
    marker = len(TRUNCATION_MARKER.encode("utf-8"))
    allowance = max(0, (budget - fixed) // len(truncatable) - marker)
    fitted: list[ContextRef] = []
    notes: list[str] = []
    for item in items:
        if Category(item.label) not in TRUNCATABLE or _size(item) <= allowance:
            fitted.append(item)
            continue
        kept = item.content.encode("utf-8")[:allowance].decode("utf-8", errors="ignore")
        notes.append(f"{item.label}: truncated from {_size(item)} bytes to {allowance}")
        fitted.append(item.model_copy(update={"content": kept + TRUNCATION_MARKER}))
    return fitted, notes
```

Replace `assemble` with:

```python
def assemble(stage: str, offered: list[ContextRef]) -> ContextPacket:
    allowed = STAGE_CONTRACTS.get(stage)
    if allowed is None:
        raise ContextContractError(
            f"unknown stage {stage!r}; declared stages are {sorted(STAGE_CONTRACTS)}"
        )
    for item in offered:
        try:
            category = Category(item.label)
        except ValueError as exc:
            raise ContextContractError(f"unknown context category {item.label!r}") from exc
        if category not in allowed:
            raise ContextContractError(
                f"stage {stage!r} may not receive category {category!r}; "
                f"its contract allows {sorted(allowed)}"
            )
    fitted, notes = _fit_to_budget(list(offered), PROMPT_BUDGET_BYTES)
    if notes:
        fitted.append(
            ContextRef(label=Category.CONTEXT_NOTES, path=None, content="\n".join(notes))
        )
    return ContextPacket(stage=stage, items=fitted)
```

The `fence` parameter is gone. No caller passed it — verify with `grep -rn "assemble(" src tests` and confirm no call site supplies a third argument.

- [ ] **Step 4: Enforce the persistent-context budget too**

`ContextPolicy.persistent_budget_bytes` was equally unenforced. In `src/dev_orchestration/workflow/bootstrap.py`, replace the body of the nested `load` function with:

```python
    def load(path: str, label: str) -> None:
        candidate = repo_root / path
        if not candidate.exists():
            excluded.append(f"{path}: missing")
            return
        try:
            reference = read_reference(repo_root, path, fence)
        except ContextContractError as exc:
            excluded.append(f"{path}: {exc}")
            return
        if label == "invariants":
            budget = project_config.context.persistent_budget_bytes
            used = sum(len(item.content.encode("utf-8")) for item in invariants)
            if used + len(reference.content.encode("utf-8")) > budget:
                excluded.append(f"{path}: over the {budget}-byte persistent context budget")
                return
            invariants.append(reference.model_copy(update={"label": label}))
        else:
            references.append(reference)
        included.append(path)
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/context/test_assembler.py tests/workflow/test_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `329 passed`.

- [ ] **Step 7: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(context): budget the prompt instead of trusting argv

compose_prompt concatenates the whole packet, diff included, into one argv
element, and Linux caps a single argument at 128 KiB. total_bytes() existed
but only a test called it. Evidence is now truncated with a recorded note;
a contract that will not fit escalates instead of going out incomplete."
```

---

## Task 11: Keep a finding's ID stable across review rounds

Finding D5. `_renumber` assigns a fresh sequential ID on every review, so one unfixed defect walks `F001 → F002 → F003` and the exhaustion message names an ID that first appeared in the final round. Identity is a fingerprint of the finding's location and summary, persisted in the run store.

**Files:**
- Modify: `src/dev_orchestration/workflow/stages.py:130-150`, `src/dev_orchestration/artifacts/store.py`
- Test: `tests/workflow/test_stages.py`, `tests/artifacts/test_store.py`

**Interfaces:**
- Consumes: `Finding`, `next_finding_id`, `RunStore.write_json_artifact`
- Produces:
  - `RunStore.finding_index(self) -> dict[str, str]`
  - `RunStore.record_finding_index(self, index: dict[str, str]) -> None`
  - `stages._fingerprint(finding: Finding) -> str`
  - `_known_finding_ids` is deleted

- [ ] **Step 1: Write the failing store test**

Append to `tests/artifacts/test_store.py`:

```python
def test_finding_index_round_trips_and_starts_empty(tmp_path):
    store = RunStore(tmp_path, "r")
    store.initialize(
        RunManifest(
            run_id="r",
            repository=str(tmp_path),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    assert store.finding_index() == {}
    store.record_finding_index({"abc": "F001"})
    assert store.finding_index() == {"abc": "F001"}
```

Reuse whatever imports the file already has for `RunStore`, `RunManifest`, `Tier`, and `ProjectClass`; add any that are missing.

- [ ] **Step 2: Write the failing stages test**

Append to `tests/workflow/test_stages.py`. This uses the file's existing `Scripted(payloads)` class, its `store` pytest fixture, and its `registry(adapter, *roles)` helper — do not introduce new ones:

```python
def _blocking(summary):
    return {
        "outcome": "CHANGES_REQUIRED",
        "findings": [
            {
                "id": "provider-made-this-up",
                "severity": "blocking",
                "summary": summary,
                "evidence_required": "a test",
                "file": "src/app.py",
                "line": 12,
            }
        ],
    }


def test_a_repeated_finding_keeps_its_id_across_rounds(store):
    adapter = Scripted([_blocking("Missing bounds check"), _blocking("Missing bounds check")])
    roles = registry(adapter, "implementation_reviewer")
    first = review_implementation(roles, "plan", "diff", "evidence", [], store)
    second = review_implementation(roles, "plan", "diff", "evidence", [], store)
    assert first.findings[0].id == "F001"
    assert second.findings[0].id == "F001"


def test_a_different_finding_gets_a_new_id(store):
    adapter = Scripted([_blocking("Missing bounds check"), _blocking("Unclosed file handle")])
    roles = registry(adapter, "implementation_reviewer")
    first = review_implementation(roles, "plan", "diff", "evidence", [], store)
    second = review_implementation(roles, "plan", "diff", "evidence", [], store)
    assert first.findings[0].id == "F001"
    assert second.findings[0].id == "F002"


def test_the_finding_index_records_why_an_id_was_reused(store):
    adapter = Scripted([_blocking("Missing bounds check"), _blocking("Missing bounds check")])
    roles = registry(adapter, "implementation_reviewer")
    review_implementation(roles, "plan", "diff", "evidence", [], store)
    review_implementation(roles, "plan", "diff", "evidence", [], store)
    assert list(store.finding_index().values()) == ["F001"]
```

Note the existing `test_plan_review_assigns_our_stable_ids` in this file must keep passing: it asserts the provider's supplied `id` is replaced by a system ID, which fingerprinting preserves.

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/artifacts/test_store.py tests/workflow/test_stages.py -k "finding" -v`
Expected: FAIL — `AttributeError: 'RunStore' object has no attribute 'finding_index'`, and the ID assertions fail with `F002` where `F001` is expected.

- [ ] **Step 4: Persist the index**

In `src/dev_orchestration/artifacts/store.py`, add after `write_versioned_json`:

```python
    def finding_index(self) -> dict[str, str]:
        """Map each finding fingerprint to the ID this run assigned it."""
        path = self.root / "review" / "finding-index.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def record_finding_index(self, index: dict[str, str]) -> None:
        self.write_json_artifact("review/finding-index.json", index)
```

- [ ] **Step 5: Assign stable IDs**

In `src/dev_orchestration/workflow/stages.py`, add `import hashlib` at the top, then replace `_renumber` and delete `_known_finding_ids` entirely:

```python
def _fingerprint(finding: Finding) -> str:
    """Identify a finding by where it is and what it says, not by its order.

    IDs are system-assigned precisely so remediation and final verification
    can refer to a finding across rounds. Renumbering sequentially on every
    review defeated that: one unfixed defect became F001, then F002, then
    F003, and the exhaustion message named an ID first seen in the last round.
    """
    raw = f"{finding.file or ''}|{finding.line or ''}|{finding.summary.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _renumber(result: ReviewResult, store: RunStore) -> ReviewResult:
    index = store.finding_index()
    assigned = list(index.values())
    renumbered: list[Finding] = []
    for finding in result.findings:
        key = _fingerprint(finding)
        finding_id = index.get(key)
        if finding_id is None:
            finding_id = next_finding_id(assigned)
            index[key] = finding_id
            assigned.append(finding_id)
        renumbered.append(finding.model_copy(update={"id": finding_id}))
    store.record_finding_index(index)
    return result.model_copy(update={"findings": renumbered})
```

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/artifacts/test_store.py tests/workflow/test_stages.py -v`
Expected: PASS.

- [ ] **Step 7: Confirm the exhaustion message now names the original ID**

Run: `.venv/bin/python -m pytest tests/workflow/test_runner.py -q`
Expected: PASS. Then confirm by hand that a persistently blocked run reports `F001`, not `F003`:

```bash
.venv/bin/python - <<'PY'
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, "tests")
from workflow.test_runner import CONFIG, CRITERIA, BLOCKING, Scripted, registry, repo, script
from dev_orchestration.workflow.runner import execute_run
tmp = Path(tempfile.mkdtemp())
out = execute_run(repo(tmp), CONFIG, registry(Scripted(script(review=BLOCKING))),
                  "always blocked", tmp / "wt", CRITERIA)
print(out.final_state, "|", out.reason)
PY
```
Expected: `RunState.ESCALATED | blocking findings ['F001'] survived 2 remediation cycles`.

- [ ] **Step 8: Run the full suite, format, lint, and commit**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "fix(review): a finding keeps its ID across remediation rounds

_renumber assigned a fresh sequential ID on every review, so one unfixed
defect became F001, F002, then F003, and unresolved_finding_ids could not
refer to anything across rounds -- which is the entire reason IDs are
system-assigned rather than taken from the provider."
```

---

## Task 12: Remove the dead code and make schemas audit evidence

Findings E1 and E2. Four constructs read as enforcement but are unreachable, and every structured call leaks a temp directory whose schema never reaches the run's artifacts.

**Files:**
- Modify: `src/dev_orchestration/workflow/runner.py`, `src/dev_orchestration/adapters/registry.py:21`, `src/dev_orchestration/workflow/invoke.py:27-41`, `src/dev_orchestration/workflow/stages.py`
- Test: `tests/workflow/test_invoke.py`

**Interfaces:**
- Consumes: `write_schema(model, directory)`
- Produces: `invoke_structured(adapter, request, model, schema_dir: Path) -> BaseModel` — `schema_dir` is now required, so no call site can silently leak

- [ ] **Step 1: Write the failing invoke test**

Append to `tests/workflow/test_invoke.py`:

```python
def test_the_generated_schema_is_written_where_the_caller_asks(tmp_path):
    adapter = ScriptedAdapter([GOOD])
    invoke_structured(adapter, request(), Classification, tmp_path / "schemas")
    assert (tmp_path / "schemas" / "classification.json").is_file()
    assert adapter.requests[0].expected_schema == tmp_path / "schemas" / "classification.json"
```

Then update every existing `invoke_structured(...)` call in that file to pass `tmp_path` (or a `tmp_path`-derived directory) as a fourth argument. Tests that do not already take `tmp_path` will need it added to their signature.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/workflow/test_invoke.py -v`
Expected: FAIL with `TypeError: invoke_structured() takes 3 positional arguments but 4 were given`.

- [ ] **Step 3: Require a durable schema directory**

In `src/dev_orchestration/workflow/invoke.py`, delete the `import tempfile` line and change the signature and first statement:

```python
def invoke_structured(
    adapter: AgentAdapter,
    request: AgentRequest,
    model: type[BaseModel],
    schema_dir: Path,
) -> BaseModel:
    """Attach a generated schema to both attempts and reject non-zero exits.

    The directory is a caller's, not a fresh mkdtemp: one temporary tree per
    structured call leaked on every run, and the schema the provider was
    actually held to never reached the run's artifacts.
    """
    schema_path = request.expected_schema or write_schema(model, schema_dir)
```

- [ ] **Step 4: Pass the run store's schema directory from every stage**

In `src/dev_orchestration/workflow/stages.py`, update all four `invoke_structured` call sites to pass `store.root / "schemas"`:

- in `classify`: `invoke_structured(registry.adapter_for("classifier"), request, Classification, store.root / "schemas")`
- in `review_plan`: `invoke_structured(registry.adapter_for("plan_reviewer"), request, ReviewResult, store.root / "schemas")`
- in `review_implementation`: `invoke_structured(registry.adapter_for("implementation_reviewer"), request, ReviewResult, store.root / "schemas")`
- in `verify`: `invoke_structured(registry.adapter_for("verifier"), request, Verification, store.root / "schemas")`

- [ ] **Step 5: Delete the dead code**

Four deletions:

1. `src/dev_orchestration/workflow/runner.py` — delete the whole `_persist_context` function (currently lines 124-139). Nothing references it.
2. `src/dev_orchestration/adapters/registry.py` — delete the `"goal_executor": RoleConfig(...)` line from `DEFAULT_ROLES`. No stage invokes that role.
3. `src/dev_orchestration/workflow/runner.py` — delete the trailing bound in the remediation loop, currently:
   ```python
            cycle += 1
            if cycle > MAX_REMEDIATION_CYCLES + 1 and implementation_review.blocking_ids():
                raise RemediationExhausted(
                    f"blocking findings {implementation_review.blocking_ids()} remained after "
                    f"{MAX_REMEDIATION_CYCLES} remediation cycles"
                )
   ```
   leaving just `cycle += 1`. The real budget is enforced inside `stages.remediate`, which raises at `cycle > MAX_REMEDIATION_CYCLES`; this second bound was off by one and unreachable. Keep the `RemediationExhausted` import — the `except` clause still needs it.
4. `src/dev_orchestration/workflow/runner.py` — remove the now-unused `Category`/`ContextRef` imports only if deleting `_persist_context` orphaned them. Run ruff to find out; do not guess.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `330 passed`.

- [ ] **Step 7: Confirm nothing dead remains**

Run:
```bash
grep -rn "_persist_context\|goal_executor\|_known_finding_ids\|mkdtemp" src tests
grep -rn "def assemble" src/dev_orchestration/context/assembler.py
```
Expected: the first command prints nothing; the second shows `def assemble(stage: str, offered: list[ContextRef]) -> ContextPacket:` with no `fence` parameter.

- [ ] **Step 8: Format, lint, and commit**

```bash
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add src tests
git commit -m "chore: delete code that reads as enforcement but never runs

_persist_context was referenced nowhere, assemble's fence parameter was
deleted on entry, goal_executor was bound to no stage, and the runner's
second remediation bound was off by one and unreachable. invoke_structured
now writes its schema into the run store instead of leaking a mkdtemp per
call, so the schema the provider was held to is audit evidence."
```

---

## Task 13: Close the two acceptance checks that assert less than they claim

Finding E3. A13's test exercises only the success path despite its name and checks file existence rather than artifact validity or base/final consistency; A10 never tests the immutability its pass condition names.

**Files:**
- Modify: `tests/workflow/test_runner.py:295-310`, `tests/test_cli_run.py:244-264`
- Test: the same files

**Interfaces:**
- Consumes: `RunStore.approve_plan`, `PlanOverwriteError`, `RunManifest`
- Produces: no new symbols

- [ ] **Step 1: Strengthen the A13 test**

In `tests/workflow/test_runner.py`, replace `test_success_and_escalation_leave_complete_audit_artifacts` entirely:

```python
def test_success_and_escalation_leave_complete_audit_artifacts(tmp_path):
    git = repo(tmp_path)
    base = git.current_commit()
    success = execute_run(
        git, CONFIG, registry(Scripted(script(tier="trivial"))), "fix", tmp_path / "wt", CRITERIA
    )
    assert success.final_state is RunState.COMPLETE_LOCAL
    for relative in (
        "request.md",
        "classification.json",
        "context.json",
        "acceptance-criteria.json",
        "verification/final-verification.json",
        "execution/execution-summary.json",
    ):
        assert (success.store_root / relative).is_file(), relative

    manifest = RunManifest.model_validate_json(
        (success.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    summary = json.loads((success.store_root / "execution" / "execution-summary.json").read_text())
    verification = json.loads(
        (success.store_root / "verification" / "final-verification.json").read_text()
    )
    assert manifest.git.base_commit == base == summary["base_commit"]
    assert manifest.git.final_commit == summary["final_commit"] != base
    assert manifest.acceptance_criteria == CRITERIA
    assert [item["criterion"] for item in verification["verdicts"]] == CRITERIA
    assert any(event["event"] == "state_changed" for event in events(success))

    escalated = execute_run(
        git,
        CONFIG,
        registry(Scripted(script(review=dict(PASS, outcome="ESCALATE")))),
        "escalate",
        tmp_path / "wt",
        CRITERIA,
    )
    assert escalated.final_state is RunState.ESCALATED
    for relative in ("request.md", "classification.json", "context.json", "manifest.json"):
        assert (escalated.store_root / relative).is_file(), relative
    escalated_manifest = RunManifest.model_validate_json(
        (escalated.store_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert escalated_manifest.terminal_reason
    assert escalated_manifest.git.final_commit is None
    assert escalated_manifest.completed_at is not None
    assert [event["event"] for event in events(escalated)][-1] in {
        "state_changed",
        "run_terminal",
    }
```

Add `from dev_orchestration.domain.run import RunManifest` to the file's imports.

- [ ] **Step 2: Strengthen the A10 test**

In `tests/test_cli_run.py`, append these assertions to the end of `test_external_plan_is_imported_hashed_reviewed_and_not_regenerated`:

```python
    run_store = RunStore(root, manifest.run_id)
    approved = run_store.root / "planning" / "approved-plan.md"
    assert approved.read_text() == PLAN
    with pytest.raises(PlanOverwriteError):
        run_store.approve_plan(run_store.root / "planning" / "plan-v1.md")
    assert approved.read_text() == PLAN
```

Add to the file's imports:

```python
import pytest

from dev_orchestration.artifacts.store import PlanOverwriteError, RunStore
```

(`RunStore` is already imported; do not duplicate it.)

- [ ] **Step 3: Run the two checks**

Run:
```bash
.venv/bin/python -m pytest tests/workflow/test_runner.py::test_success_and_escalation_leave_complete_audit_artifacts -v
.venv/bin/python -m pytest "tests/test_cli_run.py::test_external_plan_is_imported_hashed_reviewed_and_not_regenerated" -v
```
Expected: both PASS. If the A13 escalation assertions fail, that is a real defect in escalation-path artifacts — fix the runner, not the test, and report what was missing.

- [ ] **Step 4: Run the full suite, format, lint, and commit**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff format src tests && .venv/bin/ruff check .
git add tests
git commit -m "test: assert what A10 and A13 actually claim

A13's test was named for success and escalation but ran only success, and
checked is_file() rather than the base/final consistency its pass condition
names. A10's pass condition says the approval is immutable; nothing tried
a second approval."
```

---

## Task 14: Regenerate the acceptance evidence against a real commit

Finding D1. The evidence must name the commit that produces it.

**Files:**
- Modify: every file under `.superpowers/sdd/2026-08-29-m2-the-loop/validation/`

**Interfaces:**
- Consumes: the acceptance table in `docs/superpowers/plans/2026-08-29-m2-the-loop-adversarial-review-2026-08-29.md`
- Produces: `A1.log` – `A17-guards.log` and `README.md`, each naming the true `HEAD`

- [ ] **Step 1: Confirm a clean tree and capture the real SHA**

Run:
```bash
git status --porcelain
git rev-parse HEAD
.venv/bin/python -m pytest -q
```
Expected: empty status; a SHA on `fix/m2-remediation`; all tests pass. If the tree is dirty, **stop** — evidence must not be generated from uncommitted state again.

- [ ] **Step 2: Regenerate every acceptance log**

Two acceptance checks now name renamed or re-scoped tests. Use this mapping, which supersedes the A-table's original test names:

| Check | Test to run |
|---|---|
| A5 | `tests/workflow/test_runner.py::test_complete_local_persists_clean_final_commit` **and** `tests/workflow/test_runner.py::test_run_that_changes_nothing_cannot_complete_local` |
| A9 | `tests/test_cli_run.py::test_tier_override_is_effective_persisted_and_cannot_beat_minimum` (now covering accepted, rejected, and minimum cases) |

Run this script from the repository root:

```bash
set -e
SHA=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
ENV="Darwin $(uname -r) $(uname -m); Python $(.venv/bin/python -V | cut -d' ' -f2); dev-orchestration $(.venv/bin/python -c 'import importlib.metadata as m; print(m.version("dev-orchestration"))')"
OUT=.superpowers/sdd/2026-08-29-m2-the-loop/validation

log() {  # log <name> <command...>
  name=$1; shift
  { echo "commit: $SHA"; echo "branch: $BRANCH"; echo "environment: $ENV"; echo "command: $*"; } > "$OUT/$name.log"
  "$@" >> "$OUT/$name.log" 2>&1 || { echo "FAILED: $name"; exit 1; }
}

log A1  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_failing_validation_cannot_complete_local"
log A2  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_fail_or_unknown_verification_cannot_complete_local"
log A3  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_blocked_escalated_and_nonzero_agent_results_are_terminal"
log A4  .venv/bin/python -m pytest -q "tests/workflow/test_scope_check.py"
log A5  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_complete_local_persists_clean_final_commit" "tests/workflow/test_runner.py::test_run_that_changes_nothing_cannot_complete_local"
log A6  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_trivial_executor_receives_request_scope_criteria_and_constraints"
log A7  .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_profile_minimum_tier_and_protected_rules_reach_runtime"
log A8  .venv/bin/python -m pytest -q "tests/context/test_assembler.py::test_runtime_packet_records_inclusions_exclusions_conflicts_and_scope"
log A9  .venv/bin/python -m pytest -q "tests/test_cli_run.py::test_tier_override_is_effective_persisted_and_cannot_beat_minimum"
log A10 .venv/bin/python -m pytest -q "tests/test_cli_run.py::test_external_plan_is_imported_hashed_reviewed_and_not_regenerated"
log A11 .venv/bin/python -m pytest -q "tests/workflow/test_invoke.py::test_every_structured_call_attaches_generated_schema_to_both_attempts"
log A12 .venv/bin/python -m pytest -q "tests/context/test_assembler.py::test_reference_symlinks_cannot_escape_root_or_fence"
log A13 .venv/bin/python -m pytest -q "tests/workflow/test_runner.py::test_success_and_escalation_leave_complete_audit_artifacts"
log A14-pytest .venv/bin/python -m pytest -q
log A17-guards .venv/bin/python -m pytest -q tests/git/test_guards.py
```

- [ ] **Step 3: Regenerate A15 and A16 by hand**

```bash
SHA=$(git rev-parse HEAD); OUT=.superpowers/sdd/2026-08-29-m2-the-loop/validation
{ echo "commit: $SHA"; echo "branch: $(git rev-parse --abbrev-ref HEAD)";
  echo "commands: .venv/bin/ruff check . ; .venv/bin/ruff format --check src tests"; } > $OUT/A15-ruff.log
.venv/bin/ruff check . >> $OUT/A15-ruff.log 2>&1
.venv/bin/ruff format --check src tests >> $OUT/A15-ruff.log 2>&1

{ echo "commit: $SHA"; echo "branch: $(git rev-parse --abbrev-ref HEAD)";
  echo "commands: .venv/bin/dev-orch --help ; .venv/bin/python -m build --wheel ; unzip -l dist/*.whl"; } > $OUT/A16-cli-wheel.log
.venv/bin/dev-orch --help >> $OUT/A16-cli-wheel.log 2>&1
rm -rf dist && .venv/bin/python -m build --wheel >> $OUT/A16-cli-wheel.log 2>&1
unzip -l dist/*.whl | grep '\.md$' >> $OUT/A16-cli-wheel.log 2>&1
```

Confirm the A16 log lists all seven `dev_orchestration/roles/*.md` templates and that `--help` shows `doctor`, `init`, `run`, `status`, and `runs`.

- [ ] **Step 4: Rewrite the evidence README**

Replace `.superpowers/sdd/2026-08-29-m2-the-loop/validation/README.md` with:

```markdown
# M2 acceptance evidence

- Tested commit: `<paste the output of git rev-parse HEAD>`
- Branch: `fix/m2-remediation`
- Environment: `<paste the ENV string used above>`
- Working tree: clean at the tested commit. Every log below was produced from
  committed state and can be reproduced by checking that SHA out.

Checks A1–A17 come from the acceptance table in
`docs/superpowers/plans/2026-08-29-m2-the-loop-adversarial-review-2026-08-29.md`.
The remediation in `docs/superpowers/plans/2026-08-29-m2-remediation.md` renamed
two of the tests those checks name; `A5` and `A9` now cover both the positive
and the negative case, which is what their pass conditions always required.
```

- [ ] **Step 5: Verify every log names the real commit**

Run:
```bash
SHA=$(git rev-parse HEAD)
grep -L "$SHA" .superpowers/sdd/2026-08-29-m2-the-loop/validation/A*.log
grep -c "passed" .superpowers/sdd/2026-08-29-m2-the-loop/validation/A14-pytest.log
```
Expected: the first command prints nothing (every log names the SHA); the second prints `1`.

- [ ] **Step 6: Commit the evidence**

The validation directory is under `.superpowers/`, which `.gitignore` excludes. Force-add it so the evidence travels with the code:

```bash
git add -f .superpowers/sdd/2026-08-29-m2-the-loop/validation/
git add docs/superpowers/plans/
git commit -m "docs(evidence): regenerate M2 acceptance logs from a real commit

Every A*.log named 9d20cf8, which contained none of the implementation --
the entire M2 loop was uncommitted working-tree state. These logs were
produced from committed state and reproduce from the SHA they name."
```

- [ ] **Step 7: Final verification**

Run:
```bash
git status --porcelain
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check src tests
```
Expected: clean tree; all tests pass; both quality gates pass.

Then re-run the three original reproductions and confirm each is now closed:

```bash
.venv/bin/python -m pytest -q \
  "tests/workflow/test_runner.py::test_run_that_changes_nothing_cannot_complete_local" \
  "tests/workflow/test_runner.py::test_verifier_that_judges_no_criteria_cannot_complete_local" \
  "tests/workflow/test_runner.py::test_ignored_out_of_scope_write_escalates_the_run" \
  "tests/workflow/test_runner.py::test_override_may_not_lower_the_tier_below_the_classification"
```
Expected: `4 passed`.

---

## Self-review

**Spec coverage.** Every finding maps to a task: B1→3, B2→4, B3→1+2, B4→5, D1→0+14, D2→10, D3→9, D4→6, D5→11, D6→7, D7→8, E1→10+12, E2→12, E3→13, E4→3. No finding is unaddressed.

**Deliberately out of scope.** Two things the review noted are left alone on purpose, and both should be recorded as follow-up rather than silently dropped:

- `RunState.AWAITING_APPROVAL` and `RunState.APPROVED`, and the `human_approval` stage contract, remain unwired. M2 implements only the `trivial` and `standard` tiers, neither of which has a human approval gate; wiring it belongs with the `substantial` tier in a later milestone.
- Validation commands still run inside the worktree the agent just edited, so an in-scope agent could in principle weaken its own test command. The scope fence bounds this, and fixing it properly means running validation from a clean checkout of the reviewed tree — a design change, not a defect fix.

**Test count arithmetic.** Baseline 310. Task 1 +3, Task 2 +5, Task 3 +3 net (three git tests added, one runner test replaced one-for-one), Task 4 +3, Task 5 +3, Task 6 +3, Task 7 +2, Task 8 +2, Task 9 +4, Task 10 +3, Task 11 +4, Task 12 +1. Expected final total: **346**. The per-task expected counts in the steps above are cumulative and assume the tasks run in order. If a count disagrees by more than the number of tests that task adds, stop and find out why before continuing.

**Ordering constraints.** Task 0 must run first. Task 2 depends on Task 1's `ignored_paths`. Task 3 must precede Tasks 4, 7, and 13, because it changes the shared `Scripted` fake that those tasks' tests rely on. Task 7 Step 6 changes the `default_registry` monkeypatch signatures that Task 9 requires. Task 14 must run last.

**Type consistency check.** `ignored_paths` returns `list[str]` and both `check_ignored_writes` and the runner consume it as such. `commit_snapshot(message, paths)` keeps its two-argument shape, so the runner call site changes only in what it passes. `_effective_tier` keeps its three-argument signature and gains a raise. `assemble` loses a parameter no caller supplied. `invoke_structured` gains a required fourth parameter, and all four stage call sites plus every test call site are updated in Task 12. `default_registry`'s two parameters are both optional, so `default_registry()` in Task 9's second test is valid.
