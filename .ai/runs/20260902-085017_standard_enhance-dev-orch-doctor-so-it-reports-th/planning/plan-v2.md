## Revised plan

1. Update `src/dev_orchestration/doctor.py`.

   - Import `Tier` and `TIER_STAGES`.
   - Add a config-independent check named `workflow tier registry` in `run_checks()`, after adapter checks and before `_repository_checks(cwd)`. This ensures it appears even when the current directory is not a repository or `.ai/project.yaml` is missing or malformed.
   - Derive capability sets without duplicating tier definitions:
     - `supported_set = set(TIER_STAGES)`
     - `unsupported_set = set(Tier) - supported_set`
     - Render both sets by iterating `Tier` in enum/risk order: `trivial`, `standard`, `substantial`, `high_risk`.
     - Render an empty set as `none`.
   - Set this check’s `ok` value to `True`, meaning registry introspection succeeded. Use this exact current detail:
     - `registry-supported=trivial, standard, substantial; registry-unsupported=high_risk; substantial end-to-end execution requires its effective human-approval gate to be off; with the gate on, it pauses at AWAITING_APPROVAL and has no implemented resume path; approval.high_risk does not enable a high_risk execution path`
   - Include the substantial caveat only while `Tier.SUBSTANTIAL` is registry-supported, and the `approval.high_risk` caveat only while `Tier.HIGH_RISK` is registry-unsupported, preventing stale hardcoded capability claims.
   - After `.ai/project.yaml` validates successfully, append a separate check named `human approval policy`, with `ok=True` and exact detail:
     - `approval.substantial=<true|false>; approval.high_risk=<true|false>`
     - Render booleans in lowercase.
   - Keep the approval-policy check inside `_repository_checks()` because it reports validated project configuration. It must remain absent when configuration is missing or invalid.
   - Amend the module docstring to clarify that failed checks name their next action, while successful checks may report policy or capability limitations. Do not alter `Check`, `render()`, existing diagnostics, or the configuration early returns.
   - Do not add an approval/resume mechanism. Current behavior shows that gated runs return at `AWAITING_APPROVAL` ([runner.py](/Users/andrewodonnell/GitRepos/dev-orchestration/src/dev_orchestration/workflow/runner.py:467)), while the CLI exposes no approval/resume command ([cli.py](/Users/andrewodonnell/GitRepos/dev-orchestration/src/dev_orchestration/cli.py:73)).

2. Extend `tests/test_doctor.py`.

   - Add a configured-policy test using explicit non-default values:
     - `approval.substantial: true`
     - `approval.high_risk: false`
     - Assert `human approval policy.ok is True`.
     - Assert exact detail: `approval.substantial=true; approval.high_risk=false`.
   - Add a separation test with both approval settings explicitly `true`:
     - Assert the policy check reports both enabled.
     - Independently assert the tier check reports `high_risk` as registry-unsupported.
     - Assert it says `approval.high_risk` does not enable that path.
     - Assert gated substantial execution pauses at `AWAITING_APPROVAL` with no implemented resume.
   - Add a registry-derivation test:
     - Compute expected supported and unsupported lists from `set(TIER_STAGES)` and `set(Tier) - set(TIER_STAGES)`.
     - Assert the detail lists match those derived sets in enum order.
     - Temporarily monkeypatch the doctor module’s registry view—for example, add `Tier.HIGH_RISK`—and assert the reported split changes accordingly and the stale high-risk caveat disappears.
   - Add a render-level test asserting both new diagnostics render with `✓`, including their exact names and details. The marker means the relevant introspection/configuration read succeeded, not that every tier executes end-to-end.
   - Extend the existing missing-config and malformed-config tests without changing their current assertions:
     - `workflow tier registry` is present.
     - `human approval policy` is absent.
     - Existing `project config` failure details remain unchanged.
   - Keep the existing hermetic repositories, `_by_name`, and injected fake adapters.

3. Validate the change.

   Run in this order:

   1. `.venv/bin/python -m pytest -p no:cacheprovider tests/test_doctor.py`
   2. `.venv/bin/ruff check --no-cache src tests`
   3. `.venv/bin/ruff format --check --no-cache src tests`
   4. `.venv/bin/python -m pytest -p no:cacheprovider`
   5. `git status --short`

   The final status must show changes only to:

   - `src/dev_orchestration/doctor.py`
   - `tests/test_doctor.py`

4. Acceptance criteria.

   - `dev-orch doctor` reports the validated project values of `approval.substantial` and `approval.high_risk` on a distinct policy line.
   - It independently reports registry-supported and registry-unsupported tiers in risk order.
   - It identifies `trivial`, `standard`, and `substantial` as having registry paths and `high_risk` as lacking one, based entirely on `TIER_STAGES`.
   - It does not claim unconditional substantial end-to-end capability: an effective approval gate pauses the run at `AWAITING_APPROVAL`, and no resume path is implemented.
   - It states that enabling `approval.high_risk` does not create a high-risk execution path.
   - The workflow-tier diagnostic remains available when project configuration is missing or malformed; the policy diagnostic does not.
   - Existing diagnostics, their wording, rendering, and configuration early returns remain unchanged apart from the two new lines and the clarifying module docstring.
   - All focused and repository-required validation commands pass.
   - No files outside the two approved paths change.

5. Scope and recovery.

   - Do not modify `workflow/tiers.py`, the runner, CLI, state machine, configuration models, `.ai/`, or any other path.
   - If accurate behavior would require implementing approval/resume support or changing tier architecture, stop and escalate that as separate scope.
   - If compatibility cannot be preserved, remove only the newly introduced hunks from the two authorized files. Do not reset, stash, overwrite unrelated work, or weaken existing tests.