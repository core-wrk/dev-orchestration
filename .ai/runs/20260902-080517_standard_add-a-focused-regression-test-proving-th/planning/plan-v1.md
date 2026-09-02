1. Identify the existing test module covering validation-command deny-list enforcement and its established assertion/fixture patterns.

2. Add one focused regression test in that test module using a validation command that contains a prohibited remote-operation token, then assert registration or validation fails closed with the expected rejection.

3. Keep the change test-only: modify nothing outside `tests/`, with no production-code, policy, configuration, or workflow changes.

4. Run the required validation gates:

   - `.venv/bin/ruff check --no-cache src tests`
   - `.venv/bin/ruff format --check --no-cache src tests`
   - `.venv/bin/python -m pytest -p no:cacheprovider`

5. Review the final diff to confirm it contains only the regression test and that the test directly exercises the protected deny-list behavior.

6. Acceptance criteria:

   - The prohibited remote-operation token is rejected.
   - The regression test would fail if that deny-list protection were removed or bypassed.
   - All required validation gates pass.
   - No production code is changed.

7. Recovery: if the new test exposes a production defect, stop and report it rather than changing production code; if the test itself is incorrect, revert only the new test changes.