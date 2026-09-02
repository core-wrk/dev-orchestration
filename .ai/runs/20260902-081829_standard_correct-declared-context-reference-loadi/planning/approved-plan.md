## Revised plan

1. Establish authority and preconditions read-only.

   - Treat this as a specification-conformance fix, not a security-policy retirement. The target behavior is already mandated by [AGENTS.md](/Users/andrewodonnell/GitRepos/dev-orchestration/AGENTS.md:49) and reiterated by the design’s governing explanation at [design §6](/Users/andrewodonnell/GitRepos/dev-orchestration/docs/superpowers/specs/2026-08-25-dev-orchestration-v1-design.md:194).
   - Identify the three stale statements, without editing them:
     - [design §6 enforcement point 1](/Users/andrewodonnell/GitRepos/dev-orchestration/docs/superpowers/specs/2026-08-25-dev-orchestration-v1-design.md:189);
     - [M2 mutation checklist item 4](/Users/andrewodonnell/GitRepos/dev-orchestration/docs/superpowers/plans/2026-08-29-m2-the-loop.md:885);
     - [M2 remediation call site](/Users/andrewodonnell/GitRepos/dev-orchestration/docs/superpowers/plans/2026-08-29-m2-remediation.md:1586).
   - Drop the requirement to create or update an approval record. No `.ai/` or documentation artifact is a prerequisite or implementation deliverable.
   - Disclose the three-line documentation divergence in the final handoff and defer its correction to separately approved documentation work.
   - Require `git status --short` to be empty immediately before implementation. If it is not, stop without modifying, stashing, or working around existing changes.
   - Stop if repository authority has changed, the requested behavior would require broader policy changes, or implementation cannot remain within the five-file allowlist.

2. Separate declared reference reads from modification authorization in `assembler.py`.

   - Change the public signature to:
     `read_reference(repo_root: Path, rel_path: str) -> ContextRef`.
   - Remove the `ScopeFence` import and all include/exclude checks.
   - Update the function docstring or adjacent comment to state:
     - the scope fence governs modification only;
     - the caller must supply an explicitly configured reference;
     - this function enforces lexical containment, resolved containment, and symlink identity.
   - Parse `rel_path` with `PurePosixPath`.
   - Before filesystem resolution, reject:
     - absolute POSIX paths;
     - any path containing a `..` component, even when normalization would remain inside the repository.
   - Use this exact construction to avoid Darwin’s `/var` → `/private/var` alias problem:

     ```python
     resolved_root = repo_root.resolve()
     configured = PurePosixPath(rel_path)
     candidate = Path(os.path.normpath(resolved_root.joinpath(*configured.parts)))
     target = candidate.resolve()
     ```

   - Require `target` to remain beneath `resolved_root`.
   - Require `target == candidate`. This permits a symlinked spelling of `repo_root` because the root is resolved first, while rejecting final-component and ancestor-directory symlinks beneath that root.
   - Require a real regular file and guarded UTF-8 reading. Convert resolution, missing-file, non-file, symlink-loop, and read failures into `ContextContractError`.
   - Do not discover or load undeclared repository files.

   Expected outcomes:

   | Input | Outcome |
   |---|---|
   | Absolute path inside or outside the repository | Reject before resolution |
   | `../outside.txt` | Reject before resolution |
   | `src/../.ai/context.md` | Reject before resolution |
   | `.ai/context.md` | Read when explicitly declared and a non-symlink regular file |
   | File omitted from modification includes | Read when explicitly declared |
   | File matching modification excludes | Read when explicitly declared |
   | Symlink beneath the repository to an outside target | Reject |
   | Symlink beneath the repository to another repository file | Reject |
   | Ancestor-directory symlink within the configured path | Reject |
   | Repository root reached through a symlinked system/root spelling | Allow |
   | Ordinary repository-relative regular file | Read |

3. Remove modification-fence coupling from runtime context loading.

   - Change the signature in `bootstrap.py` to:

     ```python
     resolve_runtime_context(
         repo_root: Path,
         project_config: ProjectConfig,
         active_profiles: list[str],
     ) -> RuntimeContext
     ```

   - Make the nested loader call `read_reference(repo_root, path)`.
   - Delegate path validation to `read_reference`; do not use a preliminary filesystem check that follows configured symlinks before validation.
   - Continue recording missing, invalid, non-file, and unreadable declared references in `RuntimeContext.excluded`.
   - Preserve:
     - loading only `context.persistent` and active-profile `context_refs`;
     - persistent byte budgets;
     - inclusion and exclusion reporting;
     - profile constraints and minimum tiers;
     - conflict reporting and `fail_closed`.

4. Update callers and imports mechanically.

   - Update `runner.py` to use the three-argument runtime-context signature.
   - Retain runner-side `ScopeFence` construction and usage for modification enforcement, execution prompt scope context, manifests, and `context.json` metadata.
   - Update affected tests to the new signatures.
   - Remove unused `ScopeFence` imports only from `assembler.py` and test modules that no longer need them.
   - Do not change `scope.py` or `workflow/scope_check.py`.

5. Rewrite and extend assembler tests.

   In `tests/context/test_assembler.py`:

   - Replace the opposing fence-rejection assertion with proof that explicitly configured regular files load regardless of modification includes or excludes.
   - Retain an ordinary successful-read test using the two-argument signature.
   - Add lexical rejection cases for:
     - absolute inside path;
     - absolute outside path;
     - escaping `../`;
     - internally normalizing traversal such as `src/../.ai/context.md`.
   - Test separately:
     - an outside-repository symlink;
     - an in-repository final-component symlink;
     - an in-repository ancestor-directory symlink.
   - Add a regression proving that a repository root supplied through a symlink resolves first and still permits an ordinary file. Together with pytest’s Darwin `tmp_path`, this constrains the `/var` versus `/private/var` behavior.
   - Preserve or add focused missing-file and directory cases.
   - Simulate an unreadable file deterministically by making `Path.read_text` raise `OSError`/`PermissionError`, avoiding permission tests that can pass under privileged users.
   - Update the runtime reporting/conflict test while retaining inclusion, exclusion, profile constraints, budget behavior, and `fail_closed`.

6. Add realistic bootstrap integration coverage.

   In `tests/workflow/test_bootstrap.py`, construct a repository-like fixture where:

   - `scope.include` contains source/test paths but not root `AGENTS.md`;
   - `scope.exclude` contains `.ai/`;
   - `context.persistent` declares `AGENTS.md`, `.ai/context.md`, and a missing reference;
   - an active profile declares another regular reference outside modification scope.

   Assert that:

   - `AGENTS.md` and `.ai/context.md` load as invariants;
   - both appear in `RuntimeContext.included`;
   - neither is excluded because of modification scope;
   - the profile reference loads as a reference;
   - the missing reference is reported in `excluded`;
   - persistent-budget, minimum-tier, profile-constraint, and conflict-policy behavior remains unchanged.

7. Verify reference behavior and modification enforcement.

   Run:

   ```text
   .venv/bin/python -m pytest -p no:cacheprovider tests/context/test_assembler.py tests/workflow/test_bootstrap.py
   .venv/bin/python -m pytest -p no:cacheprovider tests/test_scope.py tests/workflow/test_scope_check.py
   .venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_runner.py
   ```

   Then exercise the repository’s real configuration read-only:

   ```python
   from pathlib import Path

   import yaml

   from dev_orchestration.config.models import ProjectConfig
   from dev_orchestration.workflow.bootstrap import resolve_runtime_context

   root = Path.cwd().resolve()
   config = ProjectConfig.model_validate(
       yaml.safe_load((root / ".ai/project.yaml").read_text(encoding="utf-8"))
   )
   runtime = resolve_runtime_context(root, config, [])
   required = {"AGENTS.md", ".ai/context.md"}

   assert required <= set(runtime.included)
   assert all(
       not any(item.startswith(f"{path}:") for item in runtime.excluded)
       for path in required
   )
   print("included:", runtime.included)
   print("required exclusions: none")
   ```

   Record the output in validation evidence and the final handoff, not in a repository file.

   Finally run all required gates:

   ```text
   .venv/bin/ruff check --no-cache src tests
   .venv/bin/ruff format --check --no-cache src tests
   .venv/bin/python -m pytest -p no:cacheprovider
   ```

8. Perform independent implementation review and final verification.

   The independent reviewer must confirm from the diff and command evidence that:

   - declared reads consult neither modification includes nor excludes;
   - only explicitly configured paths are loaded;
   - absolute paths, every `..` component, repository escapes, and symlinks beneath the resolved repository root are rejected;
   - resolving `repo_root` first avoids false rejection on Darwin;
   - budgets, reporting, profile behavior, and conflicts are unchanged;
   - the runner still constructs, communicates, records, and enforces its modification fence;
   - tracked, untracked, staged, ignored, deleted, and type-changing out-of-scope writes remain violations;
   - the real `.ai/project.yaml` loads both persistent references successfully;
   - the diff contains exactly:

     ```text
     src/dev_orchestration/context/assembler.py
     src/dev_orchestration/workflow/bootstrap.py
     src/dev_orchestration/workflow/runner.py
     tests/context/test_assembler.py
     tests/workflow/test_bootstrap.py
     ```

   - no `.ai/` or `docs/` file was modified;
   - the final handoff explicitly lists the three stale documentation lines.

   The independent review is returned through the review/handoff process; it is not written into the task diff. This revised plan itself must still receive independent review before approval.

9. Recovery and stop conditions.

   - If an unsafe-read or modification-scope regression appears, stop before completion.
   - Reverse only task-owned hunks in the five allowed files using targeted patches.
   - Do not use destructive Git reset/checkout operations.
   - Do not edit persistent run data, documentation, generated artifacts, or unrelated work.
   - If clean recovery would touch pre-existing work or exceed the allowlist, stop and escalate.