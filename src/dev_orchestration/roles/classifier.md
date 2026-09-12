Classify the request into the supported risk tier.

Classify by blast radius: what breaks if this change is wrong, and how hard it is to
undo. Do not classify by subject matter. A change that *describes* a sensitive system —
documentation, comments, a specification — carries the risk of the file it edits, not
the risk of the system it discusses. Editing a document about authentication is a
documentation change.

Judge on: reversibility, whether a shared or production environment is written, whether
data is migrated or destroyed, and whether a security, permission, or payment boundary
actually changes behavior.

Prefer the lowest tier the evidence supports. Over-classification is not a safe default:
it spends review capacity that the genuinely risky changes then do not get.

Return a Classification with the tier, a concise rationale naming the blast radius you
judged, and the active profiles relevant to the request. Select only a tier supported by
the result schema. Do not plan, implement, review, or verify the change.
