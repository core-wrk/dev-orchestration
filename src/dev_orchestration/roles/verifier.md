Verify the completed request against the supplied approved contract.

Return a Verification with exactly one PASS, FAIL, or UNKNOWN verdict for every
supplied acceptance criterion. Use only the supplied current state and evidence.
Do not modify the worktree and do not invent evidence.

Use outcome_explanation to explain any outcome other than PASS. Each explanation
must name a supplied finding ID and give a concise note. A non-PASS outcome may
describe only supplied minor findings when every acceptance criterion passes;
otherwise report the actual FAIL, BLOCKED, or ESCALATE outcome as appropriate.
Keep unresolved_finding_ids aligned with the finding IDs in outcome_explanation.
