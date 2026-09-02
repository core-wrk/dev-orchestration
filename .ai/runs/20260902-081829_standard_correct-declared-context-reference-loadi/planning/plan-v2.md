## Revised plan

1. Establish the security-boundary decision before implementation.

   - Record in the independent review/approval record that context assembly is retiring scope-fence enforcement point 1 from design §6: modification scope will no longer authorize declared context reads.
   - Record the replacement invariant: only explicitly configured, lexically repository-relative, non-symlink regular files may be loaded.
   - Acknowledge that the existing design and M2 plan still describe excluded paths as unreadable. Because this task’s change allowlist is restricted to `src/` and `tests/`, accept that temporary documentation divergence explicitly and defer documentation correction to separately approved work.
   - Do not edit `docs/` or `.ai/` during implementation. If the approval record does not explicitly accept this security-policy change and documentation divergence, stop and escalate.

2. Separate reference-read validation from modification authorization in `src/dev_orchestration/context/assembler.py`.

   - Change the public signature to:
     `read_reference(repo_root: Path, rel_path: str) -> ContextRef`.
   - Remove the `ScopeFence` import and both include/exclude checks. Declared references must not consult either `scope.include` or `scope.exclude`.
   - Add an explicit lexical validation step before filesystem resolution:
     - Parse the configured path as repository-relative POSIX path data.
     - Reject it when it is absolute.
     - Reject it when any path component is `..`, including traversal that ultimately remains inside the repository.
   - Resolve the repository root and candidate target, then require the resolved target to remain beneath the resolved repository root.
   - Prohibit symlink redirection by requiring the resolved target to equal the normalized lexical candidate. This rejects both final-component and ancestor-directory symlinks, including in-repository redirection to `.git/`, `.ai/runs/`, or another declared/excluded file.
   - Retain regular-file validation and guarded UTF-8 reading, converting missing, non-file, and unreadable-file failures into `ContextContractError`.

   Expected path outcomes:

   | Input | Expected outcome |
   |---|---|
   | Absolute path to a file inside the repository | Rejected before resolution |
   | Absolute path outside the repository | Rejected before resolution |
   | `../outside.txt` | Rejected before resolution |
   | `src/../.ai/context.md` | Rejected before resolution despite remaining inside the repository |
   | `.ai/context.md` | Allowed when explicitly declared and a real regular file |
   | Symlink to a target outside the repository | Rejected |
   | Symlink to another in-repository file | Rejected |
   | Ordinary declared repository-relative file | Read successfully regardless of scope include/exclude |

3. Remove modification-fence coupling from runtime context loading.

   - Change the signature in `src/dev_orchestration/workflow/bootstrap.py` to:
     `resolve_runtime_context(repo_root: Path, project_config: ProjectConfig, active_profiles: list[str]) -> RuntimeContext`.
   - Update its nested loader to call `read_reference(repo_root, path)` without a fence.
   - Preserve:
     - loading only paths declared by `context.persistent` or active-profile `context_refs`;
     - persistent-context byte budgets;
     - inclusion and exclusion reporting;
     - missing, non-file, and unreadable reference handling;
     - profile constraints and minimum tiers;
     - conflict reporting and `fail_closed` behavior.
   - In `src/dev_orchestration/workflow/runner.py`, update the context-loading call to the three-argument signature.
   - Retain `ScopeFence` creation in the runner because it remains required by `_run_worktree_stage`, `_run_validation_stage`, diff and ignored-write enforcement, execution prompt scope context, manifest data, and `context.json` metadata.

4. Update every affected call site and import mechanically.

   - Update calls currently present in:
     - `src/dev_orchestration/workflow/bootstrap.py`;
     - `src/dev_orchestration/workflow/runner.py`;
     - `tests/context/test_assembler.py`;
     - `tests/workflow/test_bootstrap.py`.
   - Remove now-unused `ScopeFence` imports from `assembler.py` and affected test files.
   - Do not remove the `ScopeFence` import from `runner.py` or change `scope.py`/`workflow/scope_check.py`.

5. Rewrite the opposing assertions and add focused reference-security tests in `tests/context/test_assembler.py`.

   - Replace `test_read_reference_refuses_a_path_outside_the_fence` with an assertion that an explicitly requested regular file reads successfully without consulting modification include/exclude.
   - Rename/update the ordinary successful-read test for the new two-argument signature.
   - Split `test_reference_symlinks_cannot_escape_root_or_fence`:
     - retain the outside-repository symlink rejection assertion with unchanged security semantics;
     - replace the excluded-target assertion with rejection of every in-repository symlink redirect, independent of modification scope.
   - Add explicit rejection cases for:
     - an absolute path pointing inside the repository;
     - an absolute path pointing outside it;
     - escaping `../` traversal;
     - in-repository traversal such as `src/../.ai/context.md`.
   - Preserve or add focused cases for missing files, directories/non-files, and read failures.
   - Update the runtime-context reporting/conflict test to the fence-free signature while retaining inclusion, exclusion, profile constraint, budget, and fail-closed conflict assertions.

6. Add realistic integration coverage in `tests/workflow/test_bootstrap.py`.

   - Use a fixture mirroring the repository policy:
     - `scope.include` covers source/test paths but not root `AGENTS.md`;
     - `scope.exclude` contains `.ai/`;
     - `context.persistent` declares both `AGENTS.md` and `.ai/context.md`.
   - Assert both files load as `invariants`, appear in `RuntimeContext.included`, and are not excluded solely because one misses the include list and the other matches the exclude list.
   - Add or retain an explicitly declared active-profile reference outside modification scope and assert it loads as a reference.
   - Retain minimum-tier, persistent-budget, missing-reference, inclusion/exclusion, and conflict-policy behavior.
   - Update the existing bootstrap call at the former four-argument call site to the final three-argument signature.

7. Verify reference behavior and unchanged modification enforcement.

   Run focused context tests:

   ```text
   .venv/bin/python -m pytest -p no:cacheprovider tests/context/test_assembler.py tests/workflow/test_bootstrap.py
   ```

   Run scope regressions:

   ```text
   .venv/bin/python -m pytest -p no:cacheprovider tests/test_scope.py tests/workflow/test_scope_check.py
   ```

   Run the runner tests that prove out-of-scope tracked and ignored writes still escalate, then run the complete runner module:

   ```text
   .venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_runner.py
   ```

   Finally run all required gates:

   ```text
   .venv/bin/ruff check --no-cache src tests
   .venv/bin/ruff format --check --no-cache src tests
   .venv/bin/python -m pytest -p no:cacheprovider
   ```

8. Perform independent implementation review and final verification.

   Confirm from the diff and test evidence that:

   - Declared reads consult neither modification includes nor excludes.
   - Undeclared files are not discovered or loaded automatically.
   - Absolute paths, all `..` traversal, repository escapes, and all symlink redirection are rejected.
   - Persistent budgets, audit reporting, profile behavior, and fail-closed conflicts are unchanged.
   - The runner still constructs, reports, prompts with, and enforces the modification fence.
   - Out-of-scope tracked, untracked, staged, ignored, deleted, and type-changing writes remain violations.
   - The diff is limited exactly to:
     - `src/dev_orchestration/context/assembler.py`;
     - `src/dev_orchestration/workflow/bootstrap.py`;
     - `src/dev_orchestration/workflow/runner.py`;
     - `tests/context/test_assembler.py`;
     - `tests/workflow/test_bootstrap.py`.
   - The accepted documentation divergence is disclosed in the final handoff rather than represented as specification consistency.

9. Recovery.

   - Require a clean worktree before implementation; do not modify, stash, or work around pre-existing uncommitted work.
   - If validation exposes an unsafe-read or modification-scope regression, stop before completion and reverse only task-owned hunks in the five allowed files.
   - Do not use destructive Git reset/checkout operations, edit persistent run data, or perform migration/generated-artifact cleanup.