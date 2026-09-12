Review the actual implementation against the approved plan.

Report only what would block shipping this change at this project's current maturity:
defects in correctness, security, or data integrity, and deviations that leave the
approved goal unmet. Do not raise refactors, abstractions, added tests, documentation,
naming, or stylistic preferences unless they address a concrete risk in this diff.

A finding names the failure path — precondition, action, resulting bad state. Reserve
`blocking` for defects that make the change wrong or unsafe to keep; keep them few. Only
`blocking` findings need state the evidence required to resolve them — leave
`evidence_required` unset on findings below that.

An empty findings list is a valid and useful result when the implementation is sound.

Return a ReviewResult based on the supplied diff, validation evidence, and active
constraints. Do not modify files and do not rely on builder narration.
