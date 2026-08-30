# M2 acceptance evidence

- Tested code commit: `6452ed5f632169580ee308a320b94f5310b288f9`
- Evidence commit: the commit that adds this validation directory
- Branch: `fix/m2-remediation`
- Environment: Darwin 25.5.0 arm64; Python 3.14.6; dev-orchestration 0.1.0
- Working tree: clean at the tested code commit before evidence capture.

Every log records the same tested code SHA, branch, environment, command, and
exit code. The dispatcher/verifier provenance record is
`dispatcher-record.json`; it records the clean-checkout boundary and the
ancestry relationship between the tested code and evidence commit.

The original M2 acceptance checks A1–A17 are in `A1.log` through
`A17-guards.log`. The remediation review's strengthened checks are recorded
in the additional logs:

- `A0-baseline.log` and `A1-baseline-commit.log` — exact recovery provenance;
- `A2-ignored-mutations.log` and `A3-final-snapshot.log` — complete mutation
  attribution and non-empty reviewed snapshots;
- `A4-criteria.log` through `A8-schemas.log` — bound criteria, finding IDs,
  prompt budget, configuration/tier joins, and durable schemas;
- `A9-collection.log` through `A13-independent.log` — collection integrity,
  static gates, disposable wheel validation, and independent evidence
  provenance.

The checks are derived from the acceptance tables in
`docs/superpowers/plans/2026-08-29-m2-the-loop-adversarial-review-2026-08-29.md`
and
`docs/superpowers/plans/2026-08-29-m2-remediation-adversarial-review-2026-08-29.md`.
