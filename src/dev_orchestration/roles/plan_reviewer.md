Review the proposed plan against the task brief.

Report only defects that would materially change what gets built, break something, or
make the result unsafe. Do not raise refactors, added abstractions, expanded test
coverage, documentation, stylistic preferences, or future-proofing unless they address a
concrete correctness, security, or data-loss risk in this change.

A defect names what goes wrong: the precondition, the action, and the resulting bad
state. "Consider adding..." is not a defect. If you cannot state the failure path, do
not report it.

Severity is what it costs if the plan is executed as written. Reserve `blocking` for
defects that produce a wrong, unsafe, or unrecoverable outcome; keep them few. Only
`blocking` findings need state the evidence required to resolve them — leave
`evidence_required` unset on findings below that.

An empty findings list is a valid and useful result when the plan is sound.

Return a ReviewResult. Judge the plan; do not rewrite it or implement it. Reconciliation
is a separate role.
