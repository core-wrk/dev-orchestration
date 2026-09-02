## Proposed plan

1. Create `docs/SELF-HOSTING.md` as the sole changed file, structured as an operational guide covering prerequisites, bootstrap, workflow controls, local boundaries, validation, evidence handoff, and unresolved follow-ups.

2. Document the narrow bootstrap exception:

   - Permit initial creation of `AGENTS.md` and `.ai/project.yaml`.
   - Explain that the exception exists only to establish repository and orchestration policy.
   - State that subsequent work must obey the configured scope and protected-path boundaries.

3. Describe the self-hosted project configuration:

   - Project classification: `internal_operating_system`.
   - Active profile: `agent_orchestration`.
   - Run tier: `substantial`.
   - Relevant-context-only loading, with persistent `AGENTS.md` and `.ai/context.md` context and on-demand plans/specifications.
   - Fail-closed behavior when context sources conflict.

4. Explain the guarded substantial workflow:

   - Plan review, workflow-change review, implementation review, and final verification are mandatory.
   - Substantial work does not require approval by default.
   - High-risk actions require explicit approval.
   - Security, privacy, data, payment, architecture, or scope-boundary changes must be escalated rather than improvised.

5. Define the local-only operating boundary:

   - Local commits may be created autonomously on `ai/`-prefixed branches.
   - Pushing, merging, deploying, publishing, and production-data mutation are prohibited autonomously.
   - Clarify that validation commands must never perform remote writes or deployment.

6. Document the validation and evidence handoff:

   - Require the configured lint, formatting, and test commands for substantial runs:
     - `.venv/bin/ruff check --no-cache src tests`
     - `.venv/bin/ruff format --check --no-cache src tests`
     - `.venv/bin/python -m pytest -p no:cacheprovider`
   - Require handoff evidence listing the files changed, reviews completed, commands run, results, failures or omissions, and confirmation that no prohibited remote action occurred.
   - Include a documentation-specific scope check confirming that only `docs/SELF-HOSTING.md` changed.

7. Add a clearly labeled follow-up for the unresolved context-authority documentation conflict:

   - Record the competing authority claims neutrally.
   - Do not select, redefine, or imply a canonical authority.
   - Note that resolution belongs in a separately approved change to the relevant canonical documentation or specification.

8. Validate the proposed documentation change:

   - Review the rendered Markdown for usable heading hierarchy, readable commands, and internally consistent terminology.
   - Run `git diff --check`.
   - Confirm through the final diff/status that no source, test, configuration, protected, or canonical specification file changed.
   - Run the three required configured validation gates and capture their outcomes, even though they target `src/` and `tests/`.

9. Acceptance criteria:

   - `docs/SELF-HOSTING.md` is an actionable operational self-hosting guide.
   - Every requested policy and boundary is stated explicitly.
   - The bootstrap exception is narrow and cannot be read as a general protected-path exemption.
   - Approval behavior distinguishes substantial work from high-risk work.
   - Local commits are distinguished from prohibited push, merge, deploy, publish, and production mutation.
   - Required reviews, validation commands, and handoff evidence are documented.
   - The context-authority conflict remains explicitly unresolved.
   - The final change set contains only `docs/SELF-HOSTING.md`.

10. Recovery:

   - If the guide is incorrect or incomplete before handoff, revise only `docs/SELF-HOSTING.md`.
   - If any other file is changed accidentally, stop and report the scope violation rather than modifying, stashing, or discarding unrelated work.
   - Because the deliverable is a new documentation file, rollback consists of removing that file through the repository’s approved recovery workflow; no remote rollback is involved.