## Revised plan

1. Establish a read-only baseline with `git status --short`. Preserve all pre-existing work and make no repository changes.

2. Evaluate the exact requested behavior:

   - Denied token: `"push"`
   - Literal validation command: `"git push origin main"`
   - Validation entry point: `ValidationCommand(command=...)`

3. Confirm whether coverage already exists in [tests/config/test_models.py](/Users/andrewodonnell/GitRepos/dev-orchestration/tests/config/test_models.py:14):

   - `EXPECTED_DENIED_TOKENS` independently includes `"push"`.
   - `test_dangerous_commands_are_rejected` already supplies `"git push origin main"`.
   - The per-token tests exercise basic, substring, and case-insensitive rejection independently of the production deny-list contents.
   - Git history confirms this coverage was deliberately hardened in commits `87400f7` and `dc4d11a`.

4. Apply this decision gate:

   - If the existing coverage remains as described, stop without adding a test. Report that the requested regression is already protected and do not claim new coverage.
   - If an uncovered behavior is discovered, stop and propose a separately reviewed plan that names that precise gap. Do not add a speculative or duplicate test under this plan.

5. On the expected no-change path, run the focused existing coverage and all required validation gates:

   - `.venv/bin/python -m pytest -p no:cacheprovider tests/config/test_models.py`
   - `.venv/bin/ruff check --no-cache src tests`
   - `.venv/bin/ruff format --check --no-cache src tests`
   - `.venv/bin/python -m pytest -p no:cacheprovider`

6. Submit the result to the orchestration layer’s independent implementation-review gate. The reviewer must confirm:

   - No test or production implementation was added.
   - The exact `"push"`/`"git push origin main"` behavior is already covered.
   - No new protection is being attributed to duplicate work.
   - The final repository delta from the recorded baseline is empty.

7. Submit the reviewed result to the orchestration layer’s independent final-verification gate. The verifier must confirm the focused test and all three required repository gates passed, and recheck the final status/diff.

8. Acceptance criteria:

   - `"git push origin main"` is rejected with `ValidationError`.
   - `"push"` remains independently pinned and exercised without deriving expected cases from `DENIED_COMMAND_TOKENS`.
   - No duplicate regression test is added.
   - All validation gates pass.
   - No production code, tests, policy, configuration, or workflow files are changed.
   - Implementation review and final verification both approve the no-change outcome.

9. Recovery:

   - If existing coverage or a validation gate fails, report the defect without changing production code.
   - If any accidental task-created edit occurs, remove only that edit and verify the original baseline is restored.
   - Mutation testing is not performed on the no-change path; it would become mandatory only in a newly reviewed plan proposing genuinely new coverage.