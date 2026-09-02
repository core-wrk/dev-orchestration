## Revised plan

1. Establish the execution baseline before editing:

   - Record `git rev-parse HEAD`.
   - Record `git status --porcelain=v1 --untracked-files=all`.
   - Record `git stash list`.
   - Expect the currently observed baseline: HEAD `c6cd1fe4c86da7fae4d392c56f893f32933362a8`, clean working tree, and no stashes.
   - If the baseline differs or contains uncommitted work, stop and escalate. Do not modify, stash, commit, or work around it.

2. Confirm the target and scope:

   - The target is `ValidationCommand` and `DENIED_COMMAND_TOKENS` in `src/dev_orchestration/config/models.py`.
   - `src/dev_orchestration/git/guards.py` is out of scope because it controls runtime `GitRepo.run_git` verbs, while the brief concerns validation-command configuration.
   - Production code, policy, configuration, workflow files, and `.ai/` remain untouched.
   - The only permitted repository change is `tests/config/test_models.py`.

3. Convert the existing generic case into a dedicated, non-duplicative regression test:

   - Remove `"git push origin main"` from the parameter list in `test_dangerous_commands_are_rejected`.
   - Import `DeniedCommandError`.
   - Add a specifically named test for `"git push origin main"` that:
     - asserts `denied_tokens(command) == {"push"}`;
     - asserts `ValidationCommand(command=command)` raises `ValidationError`;
     - inspects the Pydantic error and confirms its location is `("command",)`;
     - confirms the wrapped cause is `DeniedCommandError`;
     - confirms the cause attributes rejection to `"push"`.
   - Keep `"push"` independently declared in `EXPECTED_DENIED_TOKENS`.
   - Do not duplicate the exact command in another test.

4. Demonstrate test sensitivity with an in-memory mutation:

   - In a separate Python process, import the completed test module.
   - Rebind `DENIED_COMMAND_TOKENS` in memory to the same set minus `"push"`.
   - Invoke the new dedicated test directly.
   - Require the test to fail because the command no longer produces `{"push"}` or a `ValidationError`.
   - Record the failure type and message as orchestration evidence.
   - The mutation must not edit any repository file and disappears when the process exits.

5. Run focused validation:

   - `.venv/bin/python -m pytest -p no:cacheprovider tests/config/test_models.py`
   - Expect all tests to pass after restoring normal process state.

6. Run all required repository gates:

   - `.venv/bin/ruff check --no-cache src tests`
   - `.venv/bin/ruff format --check --no-cache src tests`
   - `.venv/bin/python -m pytest -p no:cacheprovider`
   - Each command must exit successfully.

7. Submit the result to the independent implementation-review gate. The reviewer must confirm:

   - The diff is limited to `tests/config/test_models.py`.
   - The exact command occurs in one dedicated regression test, not both the dedicated and parameterized tests.
   - The test pins `"push"` as the sole matching token and `DeniedCommandError` as the underlying rejection cause.
   - No new protection is attributed to commits `87400f7` or `dc4d11a`; those commits provide only pre-existing generic coverage.
   - No production or orchestration files changed.

8. Submit the reviewed result to the independent final-verification gate. The verifier must independently:

   - Re-run the focused test and all three repository gates.
   - Confirm the recorded in-memory mutation kills the dedicated test.
   - Compare final and baseline HEAD SHAs and require them to be identical.
   - Compare final and baseline stash lists and require them to be identical.
   - Confirm there are no staged or untracked changes.
   - Confirm the sole unstaged delta is the intended change to `tests/config/test_models.py`.

9. Acceptance criteria:

   - A dedicated regression test proves `"git push origin main"` is rejected through the validation-command deny-list.
   - The test proves `"push"`—and no other denied token—causes the rejection.
   - The wrapped validation cause is `DeniedCommandError`.
   - Removing `"push"` in memory makes the dedicated test fail.
   - `"push"` remains independently pinned through `EXPECTED_DENIED_TOKENS`.
   - No duplicate exact-command case remains.
   - All validation gates pass.
   - HEAD and stash state remain unchanged; no commit is created.
   - Implementation review and final verification approve the test-only delta.

10. Recovery and stop conditions:

   - If the dedicated test exposes missing production behavior, stop and report the defect; production changes require a separately reviewed plan.
   - If an unrelated validation gate fails, report it without weakening tests or expanding scope.
   - Remove only accidental task-created working-tree edits using targeted patches.
   - If an accidental commit or any unexpected `.git` state change occurs, stop and escalate rather than rewriting history.