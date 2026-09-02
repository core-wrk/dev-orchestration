1. Update `src/dev_orchestration/context/assembler.py` to separate read authorization from the modification scope fence:
   - Remove `ScopeFence` from `read_reference`.
   - Permit explicitly declared repository-relative references even when excluded from modification scope.
   - Continue resolving paths before reading and reject absolute paths, traversal, symlinks escaping the repository, non-files, and unreadable files.

2. Update context-loading integration in `src/dev_orchestration/workflow/bootstrap.py` and `src/dev_orchestration/workflow/runner.py`:
   - Remove the modification fence from the reference-loading call chain.
   - Preserve persistent-context budgets, inclusion/exclusion reporting, profile references, and fail-closed conflict handling.
   - Keep the existing scope fence attached to modification enforcement and run metadata.

3. Add focused tests in `tests/context/test_assembler.py` and `tests/workflow/test_bootstrap.py`:
   - Verify a declared file under an excluded in-repository path can be read.
   - Verify persistent `AGENTS.md` and `.ai/context.md` are loaded as invariants despite modification exclusions.
   - Verify traversal, absolute paths, and symlinks resolving outside the repository remain rejected.
   - Preserve coverage for missing files, unreadable/non-file references, budgets, and conflict behavior.

4. Confirm modification protection remains unchanged by running the scope and scope-check regression tests, followed by the required gates:
   - `.venv/bin/ruff check --no-cache src tests`
   - `.venv/bin/ruff format --check --no-cache src tests`
   - `.venv/bin/python -m pytest -p no:cacheprovider`

5. Perform the required implementation review and final verification, confirming:
   - Explicitly configured, read-only context inside the repository loads regardless of modification scope.
   - No reference resolving outside the repository can enter a prompt.
   - Excluded paths remain prohibited modification targets.
   - Only `src/` and `tests/` are changed.

6. Recovery: if validation exposes an unsafe read or modification-scope regression, revert only the source and test changes from this task; no migration, generated artifact cleanup, or persistent-data recovery is required.