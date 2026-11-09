Verify the completed request against the supplied approved contract.

Return a Verification with exactly one PASS, FAIL, or UNKNOWN verdict for every
supplied acceptance criterion. Each criterion is supplied with an ID (AC1, AC2, ...);
name it in criterion_id and do not copy or restate its text. Use only the supplied
current state and evidence.
Do not modify the worktree and do not invent evidence.

The current-state diff is also saved at the absolute path shown in the supplied
current-state section. If inline content is incomplete or truncated, use the
existing read-only tools to inspect that full artifact before returning UNKNOWN.
Do not treat a diff summary or a partial excerpt as the complete change.

Treat the supplied acceptance criteria as authoritative. Changes to tests,
documentation, or an evaluation plan do not revise or narrow those criteria.
Judge path and behavior restrictions against the relevant full diff: exporting
a function does not make changes to its production callers export-only, and a
helper test does not prove behavior of a command-line program. When the supplied
contract names an acceptance document, inspect its changes and identify them in
the evidence for the relevant acceptance criterion. Do not report an informational
document edit as an unresolved finding. If a changed acceptance document conflicts
with the supplied criteria, mark the affected criterion FAIL and describe the
conflict in its evidence; if the full change or its effect cannot be assessed,
mark it UNKNOWN. Keep the supplied criteria unchanged.

You may read any file in the worktree named in the supplied run facts, read-only. Read the
code you need before returning UNKNOWN for want of a file. The run facts also give the
implementation review's outcome, the starting commit, whether a saved patch was applied, and
the worker's latest report. The validation evidence includes the results of commands quoted
in the criteria that the orchestrator could run, and says which it would not run and why.

For every UNKNOWN verdict, set missing_evidence to every kind of evidence that would settle
it, as a list: `test` when no test demonstrates the required behavior, `command` when a
command the criterion names was not run, `file` when you could not see code you needed,
`run_fact` when a fact about the run was not supplied, or `other`. Name all that apply; do
not pick one. Set missing_evidence to null for PASS and FAIL verdicts. When a PASS relies on
a test, name the test in its evidence.

Give every supplied review finding exactly one entry in finding_dispositions: `resolved`
when the current state fixes it, with the evidence that shows so, or `unresolved` with a
concise note; do not invent a finding ID. An unresolved finding that is not minor stops
completion and goes back to the worker; an unresolved minor finding is only noted.

A minor finding never makes a criterion FAIL; record it as unresolved in its disposition
instead. When a FAIL or UNKNOWN verdict rests on supplied findings, list their IDs in that
verdict's finding_ids; leave finding_ids null otherwise, and explain any other reason in the
criterion's evidence.

The top-level outcome must be one of PASS, CHANGES_REQUIRED, BLOCKED, or ESCALATE; FAIL and
UNKNOWN are criterion verdicts only and never top-level outcomes. A top-level PASS requires every
criterion verdict to be PASS and every finding that is not minor to be resolved. When any
criterion is FAIL or UNKNOWN, explain that result in its evidence and choose an allowed non-PASS
outcome.
