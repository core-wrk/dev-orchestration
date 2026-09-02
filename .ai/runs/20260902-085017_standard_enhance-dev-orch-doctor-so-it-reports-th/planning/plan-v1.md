1. Update `src/dev_orchestration/doctor.py`.

   - After successfully validating `.ai/project.yaml`, append an informational diagnostic for the configured human-approval policy, reporting both `approval.substantial` and `approval.high_risk` explicitly.
   - Append a separate workflow-capability diagnostic derived from the implemented tier registry in `workflow/tiers.py`, rather than duplicating tier definitions.
   - State unambiguously that `trivial`, `standard`, and `substantial` execution paths are supported, while `high_risk` execution remains unsupported even when its approval policy is enabled.
   - Preserve all existing checks, wording, rendering behavior, and early returns for missing or invalid configuration.

2. Extend `tests/test_doctor.py` with focused repository-check tests.

   - Verify configured approval values—not merely defaults—appear correctly in doctor output.
   - Verify the workflow-tier diagnostic identifies substantial execution as supported and high-risk execution as unsupported.
   - Verify approval configuration and implementation support are presented as distinct facts, preventing `approval.high_risk: true` from implying that high-risk execution is available.
   - Keep existing doctor tests and exact assertions intact; reuse the hermetic temporary-repository helpers and injected fake adapters.

3. Validate the change.

   - Run the focused doctor suite: `.venv/bin/python -m pytest -p no:cacheprovider tests/test_doctor.py`.
   - Run repository-required checks:
     - `.venv/bin/ruff check --no-cache src tests`
     - `.venv/bin/ruff format --check --no-cache src tests`
     - `.venv/bin/python -m pytest -p no:cacheprovider`

4. Acceptance criteria.

   - `dev-orch doctor` reports the effective substantial and high-risk human-approval settings from project configuration.
   - It separately reports the implemented workflow tiers.
   - Its output explicitly says substantial execution is supported and high-risk execution is unsupported.
   - Existing doctor diagnostics and rendering remain unchanged apart from the newly added lines.
   - Missing or invalid project configuration still returns the existing diagnostics without raising.
   - All focused and full validation commands pass.
   - Only `src/dev_orchestration/doctor.py` and `tests/test_doctor.py` are changed.

5. Recovery.

   - If validation fails or compatibility cannot be preserved, revert only the additions in those two files; do not modify `.ai/`, configuration policy, workflow tier definitions, or any other repository paths.