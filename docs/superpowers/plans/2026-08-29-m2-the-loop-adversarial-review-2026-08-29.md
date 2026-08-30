# Adversarial Review — M2 — The Loop Implementation Plan

**Plan reviewed:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md`
**Review date:** 2026-08-29
**Reviewer:** Codex (GPT-5)
**Repository state:** `9d20cf882cea94d17ec8370dba2309accd32fb98` (`main`, clean)
**Verification depth:** read-only static inspection — no tests, provider calls, writes outside this review, or shared-environment mutation

## Verdict

**BLOCKED**

The task decomposition is unusually concrete, but the proposed end-to-end runner defeats several controls the component tasks appear to establish. It can mark a run `COMPLETE_LOCAL` after failed validation or failed verification, cannot see uncommitted changes in its scope check or review diff, gives a trivial executor no task, and never supplies the runtime invariants/profile constraints its safety argument depends on. Resolve B1–B4 and add their negative integration tests before dispatching implementation agents; otherwise the full suite can be green while the shipped loop certifies an empty, dirty, out-of-scope, or failed run.

| | Count |
|---|---:|
| Blockers | 4 |
| Major | 6 |
| Minor | 1 |
| Unverified claims | 3 |

## Blocking findings

### B1 — Terminal success ignores every negative verdict

**Where:** plan Task 19, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4109`; source contract `../dev-orchestration-spec/IMPLEMENTATION-PLAN.md:419`

**Failure path:** a required validation command exits non-zero, an implementation review returns `BLOCKED`/`ESCALATE`, or final verification returns `FAIL`/`UNKNOWN` → the runner records the value but checks only `review.blocking_ids()` → it transitions unconditionally to `COMPLETE_LOCAL` at plan line 4159. The same path ignores the implementation worker's `AgentResult.exit_code` at line 4107. A failed run is therefore certified as complete.

**Evidence:** `all_passed(outcomes)` is used only as an event field at lines 4113–4115 and 4137–4139; the review outcome is never branched on at lines 4122–4127; the verification result is never inspected at lines 4148–4159. The canonical acceptance criterion says validation failure prevents final PASS (`../dev-orchestration-spec/IMPLEMENTATION-PLAN.md:423`), and the outcome definitions distinguish `BLOCKED` and `ESCALATE` from `PASS` (`../dev-orchestration-spec/ARCHITECTURE.md:518`).

**Resolution:** make every stage outcome a state-machine gate. Non-zero executor exit, failed required validation, `BLOCKED`, `ESCALATE`, any acceptance verdict other than `PASS`, or unresolved blocking findings must prevent `COMPLETE_LOCAL` and produce the correct terminal state and durable evidence. Add the negative tests in A1–A3; the existing happy-path test at plan lines 3915–3921 proves none of them.

### B2 — The scope fence and reviewer are blind to uncommitted work

**Where:** plan Tasks 10 and 19, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:1970`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4117`; M1 implementation `src/dev_orchestration/git/repo.py:72`

**Failure path:** the implementation worker writes `vendor/sneaky.py` but does not commit it → `GitRepo.changed_files(base_ref)` runs `git diff --name-only <base> HEAD`, which excludes index, worktree, and untracked changes (`src/dev_orchestration/git/repo.py:77`) → the fence reports no offender, the implementation-review diff is also `git diff <base> HEAD` (plan line 4121), and the runner reaches `COMPLETE_LOCAL` without creating or persisting a final commit. This directly falsifies both “actual git diff” and “ends … with a local commit” (`docs/superpowers/plans/2026-08-29-m2-the-loop.md:1975`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:3799`).

**Evidence:** the M1 `GitRepo` has a commit helper at `src/dev_orchestration/git/repo.py:72`, but Task 19 never calls it. The proposed scope tests commit every fixture mutation before checking it (`docs/superpowers/plans/2026-08-29-m2-the-loop.md:2010`, verified by static inspection), so they cannot catch the uncommitted path. The canonical flow requires local commits and a clean final state (`../dev-orchestration-spec/CLI-AND-ADAPTERS.md:354`, `../dev-orchestration-spec/CLI-AND-ADAPTERS.md:382`).

**Resolution:** define one change inventory covering commits since base, staged changes, unstaged changes, renames, deletions, and untracked files. Use it for the fence, review diff/state, execution summary, and final commit. Require a clean worktree and persist `git.final_commit` before `COMPLETE_LOCAL`; add A4–A5.

### B3 — A trivial executor receives no user request

**Where:** plan Task 19, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4090`; execution stage contract `docs/superpowers/plans/2026-08-29-m2-the-loop.md:795`

**Failure path:** classification returns `trivial` → planning is correctly skipped → `approved` remains `""` → `execute()` is called with that empty value and empty invariant/reference lists at line 4107 → the worker receives only its generic role template, an empty `APPROVED_PLAN`, and a worktree path. It cannot know that the request was “fix a typo,” so it can do nothing (or guess) and still reach the scripted PASS path.

**Evidence:** the trivial test checks only that the planner was not called (`docs/superpowers/plans/2026-08-29-m2-the-loop.md:3924`). The canonical trivial flow is request → direct implementation (`../dev-orchestration-spec/WORKFLOWS.md:17`), not request → discard request → implementation.

**Resolution:** create an immutable lightweight execution contract for trivial runs containing the original request, bounded scope, acceptance criteria, scope fence, and protected constraints. It may be represented as a task brief rather than a formal plan, but it must be the actual content passed to execution and persisted for audit. Add A6.

### B4 — Runtime context and policy resolution are shells around empty inputs

**Where:** plan architecture and Tasks 4, 8, 14, 19, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:7`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:1670`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4084`

**Failure path:** a repository exposes an `authentication` profile with a high-risk minimum and protected invariants → classification is given only project class, not available profiles; bootstrap resolves only the repository `ProjectConfig`; the runner ignores `classification.profiles`; every planning, review, and execution call passes empty invariants, references, and profile constraints → a request can run as `standard` without the profile minimum, and the workspace-write executor is never told the repository's protected constraints or scope fence. The declared context categories exist, but the safety content never enters them.

**Evidence:** `classify()` is called without `project_config.profiles.available` at plan lines 4084–4086; policy resolution gets exactly one layer at lines 1670–1674 despite the six-layer owner contract (`../dev-orchestration-spec/CONFIGURATION.md:3`); execution receives `[], []` at line 4107; plan review and implementation review receive empty profile constraints at lines 4094–4096 and 4122–4124. The execution contract in the plan omits the scope-fence category at lines 795–801 even though the canonical stage contract requires it (`../dev-orchestration-spec/WORKFLOWS.md:177`). The canonical context assembler must resolve invariants, task, active profiles, exclusions, and conflicts (`../dev-orchestration-spec/ARCHITECTURE.md:186`, `../dev-orchestration-spec/CLI-AND-ADAPTERS.md:249`).

**Resolution:** implement the configuration/context join, not only a category allow-list. Resolve all applicable layers, enforce profile minimum tiers, persist override/profile decisions, load configured persistent invariants through safe reference resolution, include the scope fence in execution, detect conflicting protected constraints, and record included/excluded context. Add A7–A8.

## Major findings

### M1 — Generated provider schemas have no production caller

**Where:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md:328`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:1198`

**Failure path:** Task 2 generates schemas and Task 5 teaches Codex to accept `expected_schema` → every structured stage builds an `AgentRequest` without `expected_schema` → `invoke_structured()` calls the adapter unchanged → Codex never receives `--output-schema`, contradicting the global constraint at plan line 26. Post-hoc Pydantic parsing remains, but provider-side enforcement is absent.

**Evidence:** static call-site inspection of Tasks 14, 15, 17, and 18 found no `write_schema`, `schema_for`, or `expected_schema` argument; Task 6 lines 1198–1218 does not add one.

**Resolution:** make `invoke_structured` (or a request factory) generate/select the model schema, attach its path to both attempts, and test the actual adapter argv for every structured role. Reject extra keys recursively, not only at the top-level schema object.

### M2 — `approved-plan.md` is mutable and reconciliation self-approves

**Where:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md:2509`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:2576`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4098`

**Failure path:** a blocking plan finding triggers reconciliation → the reconciler returns arbitrary Markdown with no structured resolution record → the runner immediately overwrites `approved-plan.md` and executes it without re-review or evidence that the blocking finding closed. The public `approve_plan()` API explicitly allows replacing an already approved contract, so later code can silently change what “approved” means.

**Evidence:** the proposed test requires later approval to replace the contract at lines 2509–2516; `approve_plan` uses ordinary `write_text` at lines 2582–2586; Task 19 approves reconciliation output immediately at lines 4098–4104. Canonical acceptance requires the approved plan to be immutable within the run (`../dev-orchestration-spec/IMPLEMENTATION-PLAN.md:322`) and reconciliation to mark approval only when all blocking findings are resolved (`../dev-orchestration-spec/WORKFLOWS.md:291`).

**Resolution:** make first approval an atomic create tied to a source version/hash; reject replacement. Require structured reconciliation dispositions and either independent re-review or deterministic evidence that every blocking finding is closed before approval.

### M3 — `--tier` and `--plan` advertise behavior they do not implement

**Where:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4217`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4480`

**Failure path:** a user passes `--tier trivial` → the CLI validates the spelling but never passes the override to `execute_run` → the classifier still chooses the effective tier and the manifest contains no override. A user passes `--plan` → `plan_file` is never used in the shown command; a prose note at line 4511 leaves review/approval/skip semantics to the implementer and adds no CLI test.

**Evidence:** line 4494 calls `stages_for(Tier(tier))` and discards the result; lines 4495–4502 omit both options. Canonical behavior requires overrides to be persisted (`../dev-orchestration-spec/CONFIGURATION.md:364`) and distinguishes unreviewed, approved, and reconciliation-required external plans (`../dev-orchestration-spec/WORKFLOWS.md:564`).

**Resolution:** thread an effective-tier override through classification/policy with minimum-tier enforcement and manifest evidence. Treat `--plan` as unreviewed by default and route it through review/reconciliation; add an explicit, separately evidenced approved mode if direct execution is required. Add A9–A10.

### M4 — The durable audit trail described by the architecture is not built

**Where:** plan file structure, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:46`; artifact owner `../dev-orchestration-spec/ARTIFACTS-AND-AUDITABILITY.md:47`

**Failure path:** the loop executes → request, classification, plan review, implementation-review rounds, validation results, execution summary, remediation artifacts, and final verification exist only as lossy event fields or not at all → `status` can report a state, but a future investigator cannot prove which inputs/results produced it.

**Evidence:** the plan creates no store APIs or files for `request.md`, `classification.json`, `plan-review-v*.json`, `execution-summary.json`, `implementation-review-v*.json`, validation result artifacts, or `final-verification.json`; these are the recommended durable evidence set at `../dev-orchestration-spec/ARTIFACTS-AND-AUDITABILITY.md:49`. Validation events persist only a boolean at plan lines 4113–4115, not the exit codes the plan says must be evidence.

**Resolution:** add artifact writers with atomic/versioned semantics and end-to-end assertions that every run outcome, including escalation, leaves enough evidence to reconstruct request, code state, validation, reviews, remediation, and terminal decision.

### M5 — Provider and unexpected failures bypass the state machine

**Where:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md:1203`, `docs/superpowers/plans/2026-08-29-m2-the-loop.md:4167`

**Failure path:** a provider exits non-zero with parseable JSON, times out, is missing, or raises an OS/subprocess error → structured invocation may accept the payload despite the exit code, while exceptions outside the four listed types escape `execute_run` → the manifest remains in a non-terminal state with no failure event.

**Evidence:** Task 6 validates only `result.output` at lines 1203–1206 and never inspects `exit_code`; Task 19 catches only `RemediationExhausted`, `ScopeViolation`, `SchemaEscalation`, and `ContextContractError` at lines 4167–4172. The adapter contract requires timeout, exit code, error categorization, and event-log evidence (`../dev-orchestration-spec/CLI-AND-ADAPTERS.md:399`).

**Resolution:** normalize adapter failures into typed workflow outcomes, reject non-zero results before schema validation, and guarantee every started run reaches `FAILED`, `BLOCKED`, or `ESCALATED` with an event and manifest update.

### M6 — Fence-aware reference reading can escape through a symlink

**Where:** `docs/superpowers/plans/2026-08-29-m2-the-loop.md:857`; current fence behavior `src/dev_orchestration/scope.py:68`

**Failure path:** an allowed repository path is a symlink to an excluded file or a file outside the repository → `fence.allows(rel_path)` accepts the lexical path → `(repo_root / rel_path).read_text()` follows the symlink → secret or out-of-scope content enters an agent prompt despite the stated enforcement point.

**Evidence:** the proposed code checks only the relative string at lines 863–871; `ScopeFence.allows` similarly validates lexical `PurePosixPath` parts (`src/dev_orchestration/scope.py:68`). The tests cover `../` but not symlinks.

**Resolution:** resolve both repository root and target, require the resolved target to remain under the resolved root, define whether in-repo symlinks into excluded paths are checked by lexical or resolved path, and add both escape tests.

## Minor findings

- **m1** Task 11 shows a `[tool.setuptools.package-data]` snippet although the repository uses Hatchling (`pyproject.toml:14`); the prose permits an equivalent, but the plan needs a Hatch-specific wheel-content test so an agent cannot add inert configuration — `docs/superpowers/plans/2026-08-29-m2-the-loop.md:2287`.

## Unverified claims

| Claim (plan §) | Why unverifiable | What would settle it |
|---|---|---|
| Baseline is 212 passing tests at `48f30a8` (Global Constraints) | Commit `48f30a8` exists and `.pytest_cache/v/cache/nodeids` contains 212 node entries, but the cache is not commit-bound evidence and this review did not run tests. | Run the full suite in a clean worktree at `48f30a8`, capture exit 0, commit SHA, Python version, and command output. |
| Real Codex and Claude invocations honor the proposed structured-output contract | No live provider call was made; static inspection cannot verify installed-CLI output envelopes or schema acceptance. | Provider smoke tests against the installed binaries, including Claude's JSON envelope and Codex nested schema, saved with versions. |
| `default_registry(config)` can be added without changing configuration semantics (Task 20) | The function does not exist and the plan gives no resolution rules for global role overrides, binary overrides, or health failures. | A specified resolver contract plus integration tests using global and repository configuration. |

## Assumptions applied

| # | Ambiguity | Assumption applied | Impact if wrong |
|---|---|---|---|
| A1 | The requested relative path was absent from the initial `dev-orchestration-spec` checkout. | The sibling `dev-orchestration` repository containing the exact path is the target. | The review would apply to the wrong repository snapshot. |
| A2 | Canonical `standard` plan review is conditional, while this M2 plan always performs it. | Always-review is an intentional stricter M2 policy. | The runner would do unnecessary review work, but not weaken safety. |
| A3 | `--plan` does not say whether the external plan is already approved. | It is unreviewed by default and must pass independent plan review/reconciliation. | A separately approved plan would take an extra review pass. |
| A4 | Task 19 applies the clean-base rule to trivial runs too. | The explicit stricter ruling at plan line 4187 stands. | Trivial runs would be more restrictive than the canonical tier definition. |
| A5 | Some repositories intentionally have no required validation command. | An empty applicable command set is allowed only when resolved policy proves none is required; it is recorded as “no required checks,” not positive test evidence. | A repository intending a mandatory check could be falsely certified. |

---

# End-State Contract

## Definition of done

1. **D1 — Outcome gates:** `COMPLETE_LOCAL` is reachable only after successful execution, all required validations pass, implementation review is not `BLOCKED`/`ESCALATE` and has no blocking findings, final verification is `PASS`, every acceptance criterion is `PASS`, and no unresolved blocking finding remains.
2. **D2 — Tier correctness:** trivial execution receives an immutable task contract containing the request, scope, acceptance criteria, fence, and protected constraints while invoking no planner or plan reviewer; standard execution receives one immutable approved plan.
3. **D3 — Policy/context join:** the run resolves and persists every applicable configuration layer, available/active profiles, minimum tier, user override, protected rules, role bindings, context inclusions, exclusions, and conflicts. Every stage receives exactly its canonical contract with non-empty required content.
4. **D4 — Complete change inventory:** fence checks, review, verification, and execution summary cover committed, staged, unstaged, deleted, renamed, and untracked paths. A successful run ends with a clean worktree and a persisted final local commit descended from the recorded base.
5. **D5 — Structured invocation:** classification, both reviews, and verification use a generated provider schema on the initial call and retry, reject provider non-zero exit, and validate the same strict Pydantic shape locally.
6. **D6 — Plan integrity:** plan versions are append-only; reconciliation records dispositions for every finding; `approved-plan.md` is atomically created from a named/hash-pinned version and cannot be replaced within the run.
7. **D7 — CLI truthfulness:** `--tier` changes and persists the effective tier subject to protected minimums; `--plan` imports, copies, hashes, records origin, and follows the declared review/approval path; escalated/failed runs return a non-success CLI result.
8. **D8 — Durable evidence:** every run persists request, classification, resolved config/roles, plan/reviews where applicable, execution summary, validation attempts with exit codes, implementation review/remediation rounds, final verification, git base/final commit, and a complete state/event timeline.
9. **D9 — M2 surface:** only trivial and standard execute; any substantial/high-risk result or protected minimum exits before implementation with a recorded escalation and no attempt to weaken the tier.

## Invariants — must hold throughout, not just at the end

1. **I1:** no push, merge, deploy, publication, remote branch deletion, production-data mutation, or destructive history rewrite is implemented or invoked.
2. **I2:** the base repository is never modified; all implementation/remediation writes occur in the isolated run worktree created from the recorded base commit.
3. **I3:** protected rules and minimum tiers only become stricter across configuration layers; repository or CLI configuration cannot weaken them.
4. **I4:** every agent request carries the active role contract and its required non-inferable invariants; read-only roles remain bound only to an adapter that enforces read-only access.
5. **I5:** validation consumes no builder narration and runs the configured commands itself in the worktree.
6. **I6:** the full change inventory is checked against the fence after every execution/remediation mutation and again before completion.
7. **I7:** no reviewed plan version is edited, and an approved contract is never overwritten.
8. **I8:** every state change and external-command failure is durably recorded; no started run is left indefinitely in a non-terminal state after an exception.
9. **I9:** tests and canonical design documents are not deleted, skipped, weakened, or rewritten to make acceptance pass.

## Prohibitions — out of bounds regardless of outcome

1. **P1:** do not make a failing validation/verifier/reviewer result pass by ignoring it, relabeling it, or changing only the event output.
2. **P2:** do not stage or commit only in-fence files while leaving out-of-fence worktree changes untracked; inventory all states directly from Git.
3. **P3:** do not hardcode canned PASS output, acceptance criteria, changed-file lists, or finding closure in production code.
4. **P4:** do not widen a scope fence, suppress a profile, lower a minimum tier, drop a protected constraint, or rebind a read-only role to make a run proceed.
5. **P5:** do not replace, edit, or approve an unreviewed plan contract to avoid resolving findings.
6. **P6:** do not use builder narration or provider exit text as validation evidence.
7. **P7:** do not add remote/destructive operations, `shell=True`, interpolated provider command strings, or the prohibited Codex bypass flag.
8. **P8:** do not treat an empty applicable validation set as evidence that tests passed; record why no command was required.

## Stop conditions — halt and escalate

1. **S1:** resolved policy or an active profile requires `substantial`/`high_risk`, approval, or a protected behavior M2 cannot implement.
2. **S2:** a configuration/context conflict cannot be resolved by canonical ownership, or a required invariant/reference cannot be read safely.
3. **S3:** the base repository is dirty, the base commit moves before worktree creation, or the run worktree does not descend from the recorded base.
4. **S4:** any change escapes the fence, the change inventory cannot account for a Git status entry, or the final worktree cannot be made clean without discarding work.
5. **S5:** a provider exits non-zero, times out, violates schema twice, or a requested read-only restriction cannot be enforced.
6. **S6:** validation fails, review returns `BLOCKED`/`ESCALATE`, remediation exceeds two resolved-policy cycles, or final verification is not an unqualified PASS.
7. **S7:** closing a finding would alter approved scope, architecture, security/authorization, privacy/data handling, payment/revenue semantics, or an external contract.

## Scope boundaries

**In scope:** the paths declared by Tasks 1–20 under `src/dev_orchestration/`, `tests/`, `schemas/`, and the minimal build configuration needed to package role templates; run evidence under `.ai/runs/` and `.superpowers/sdd/2026-08-29-m2-the-loop/`.

**Out of scope / do not touch:** the reviewed plan itself; historical M1 plan/review/progress artifacts; canonical documents in `../dev-orchestration-spec`; remote repositories; deployments; substantial/high-risk execution; human approval UI; post-M3 composite/documentation/telemetry features.

## Acceptance checks

Each test named below must be added before its command is used as evidence. Store command output with the tested commit SHA, branch, Python/CLI versions, and environment under `.superpowers/sdd/2026-08-29-m2-the-loop/validation/`.

| # | Check | Command / observation | Pass condition | Evidence artifact |
|---|---|---|---|---|
| A1 | Validation failure gates completion | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_failing_validation_cannot_complete_local -v` | Exit 0; outcome is not `COMPLETE_LOCAL`; failing command and exit code are persisted. | `validation/A1.log` + run fixture artifacts |
| A2 | Verification gates completion | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_fail_or_unknown_verification_cannot_complete_local -v` | Exit 0 for both FAIL and UNKNOWN cases; neither completes. | `validation/A2.log` |
| A3 | Review/executor outcomes gate completion | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_blocked_escalated_and_nonzero_agent_results_are_terminal -v` | Exit 0; each case reaches its specified non-success terminal state. | `validation/A3.log` |
| A4 | Fence sees all Git states | `.venv/bin/python -m pytest tests/workflow/test_scope_check.py::test_uncommitted_staged_and_untracked_out_of_scope_paths_fail -v` | Exit 0; each path is named without first committing it. | `validation/A4.log` |
| A5 | Final commit is real and clean | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_complete_local_persists_clean_final_commit -v` | Exit 0; final SHA descends from base, contains the reviewed diff, manifest matches it, status is clean. | `validation/A5.log` + run manifest |
| A6 | Trivial receives its task | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_trivial_executor_receives_request_scope_criteria_and_constraints -v` | Exit 0; packet contains all four and no planning/review role was invoked. | `validation/A6.log` |
| A7 | Policy/profile join | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_profile_minimum_tier_and_protected_rules_reach_runtime -v` | Exit 0; active profile elevates or safely escalates, is persisted, and its constraints reach allowed stages. | `validation/A7.log` + manifest |
| A8 | Context is complete and auditable | `.venv/bin/python -m pytest tests/context/test_assembler.py::test_runtime_packet_records_inclusions_exclusions_conflicts_and_scope -v` | Exit 0; conflict fails closed; exclusions and scope fence are present. | `validation/A8.log` |
| A9 | Tier override is effective | `.venv/bin/python -m pytest tests/test_cli_run.py::test_tier_override_is_effective_persisted_and_cannot_beat_minimum -v` | Exit 0 for accepted and rejected override cases. | `validation/A9.log` + manifests |
| A10 | External plan path is deterministic | `.venv/bin/python -m pytest tests/test_cli_run.py::test_external_plan_is_imported_hashed_reviewed_and_not_regenerated -v` | Exit 0; bytes/hash/origin recorded; planner skipped; reviewer runs; approval is immutable. | `validation/A10.log` + planning artifacts |
| A11 | Provider schema is actually used | `.venv/bin/python -m pytest tests/workflow/test_invoke.py::test_every_structured_call_attaches_generated_schema_to_both_attempts -v` | Exit 0; fake adapter observes the schema path on both calls and nested extras are rejected. | `validation/A11.log` |
| A12 | Symlink escape is closed | `.venv/bin/python -m pytest tests/context/test_assembler.py::test_reference_symlinks_cannot_escape_root_or_fence -v` | Exit 0 for outside-root and excluded-target links. | `validation/A12.log` |
| A13 | Durable run evidence | `.venv/bin/python -m pytest tests/workflow/test_runner.py::test_success_and_escalation_leave_complete_audit_artifacts -v` | Exit 0; required artifact set exists, validates, and references one base/final code state. | `validation/A13.log` + run artifacts |
| A14 | Full regression suite | `.venv/bin/python -m pytest -q` | Exit 0; exact pass/skip counts recorded. | `validation/A14-pytest.log` |
| A15 | Static quality gates | `.venv/bin/ruff check . && .venv/bin/ruff format --check src tests` | Both exit 0. | `validation/A15-ruff.log` |
| A16 | CLI surface and package contents | `.venv/bin/dev-orch --help && .venv/bin/python -m build && unzip -l dist/*.whl` | Exit 0; commands appear; wheel contains every `roles/*.md`; no runtime template is missing. | `validation/A16-cli-wheel.log` |
| A17 | Prohibited operations remain absent | `.venv/bin/python -m pytest tests/git/test_guards.py -v` | Exit 0, including planted literal subprocess and runtime-argv guard cases. | `validation/A17-guards.log` |

## Recovery position

All M2 work is local and reversible; no remote or shared-environment action is authorized. Each workstream ends in a focused local commit only after its targeted tests pass. If a workstream fails, stop in the isolated implementation worktree, preserve its failing evidence and diff, and return to the last passing task commit with a normal revert commit if recovery is required—never reset, stash, force-checkout, or discard unknown work. A run under test that fails after bootstrap remains preserved in a terminal `FAILED`/`ESCALATED` state with its worktree and artifacts intact for diagnosis; cleanup is a separate explicit action after evidence has been captured.

---

# Execution Routing

| # | Workstream | Owns (paths) | Depends on | Complexity | Harness fit | Human gate |
|---|---|---|---|---|---|---|
| W1 | Result models, strict schemas, invocation | `domain/findings.py`, `schemas.py`, adapter request/schema wiring, related tests | M1 adapters/registry | Moderate | Either; local fake adapters required | No |
| W2 | Context, policy, classification, tiering | `context/`, `workflow/bootstrap.py`, `workflow/tiers.py`, classification portion of `stages.py`, related tests | W1 schemas/models | High-judgment | Local repo access; no live provider needed | Yes — approve resolved configuration/context contract before W4 |
| W3 | Git inventory, scope, validation, artifacts | `git/repo.py`, `workflow/validation.py`, `workflow/scope_check.py`, `artifacts/store.py`, domain manifest, related tests | M1 git/store | High-judgment | Local sandbox with temporary Git repositories | Yes — verify fence/commit/immutability joins before W4 |
| W4 | Planning through final verification and runner | remaining `workflow/stages.py`, `workflow/engine.py`, `workflow/runner.py`, runner tests | W1–W3 | High-judgment | Local fake-adapter integration; live smoke deferred | Yes — review all negative terminal paths before CLI wiring |
| W5 | CLI, reporting, packaging, E2E evidence | `cli.py`, `workflow/reporting.py`, `adapters/registry.py`, `pyproject.toml`, CLI/package tests | W4 | Moderate | Local CLI and wheel build | No; verify at completion |

**Complexity ratings**

- **Mechanical** — path fully determined, deterministic success check, local and reversible. Smallest capable model, no checkpoint.
- **Moderate** — bounded judgment in one subsystem, clear acceptance test, recoverable. Mid-tier model, verify at completion.
- **High-judgment** — cross-cutting, ambiguous tradeoffs, irreversible or shared-environment actions, security/data-integrity blast radius. Strongest available model plus a human checkpoint before the irreversible step.

**Ordering and parallelism**

W1 can begin independently. W2 and W3 may proceed in parallel after agreeing on the manifest/config/change-inventory interfaces; they must not edit the same manifest or `stages.py` locations concurrently. W4 is serial after W1–W3 because it is the trust-boundary join this review found broken. W5 follows W4. Tasks 14–18 all modify `workflow/stages.py` and must stay in one workstream or be executed serially.

**Per-workstream done-checks**

| Workstream | Closes | Evidence to produce |
|---|---|---|
| W1 | D5; A11 | Generated schema/argv tests, non-zero provider failure test |
| W2 | D2, D3, D9; A6–A9, A12 | Profile/override/context manifests and packet evidence |
| W3 | D4, D6, validation portion of D8; A1, A4, A5, A13 | Complete Git inventory, immutable plan, validation artifacts |
| W4 | D1 and end-to-end D8; A1–A7, A13 | Success plus every negative terminal-path run artifact |
| W5 | D7 and packaged role templates; A9, A10, A14–A17 | CLI output, wheel listing, full suite and guard logs |

---

## Open questions

No dispatch-blocking questions remain after applying assumptions A1–A5. The only deferred evidence question is real-provider compatibility; it belongs to a version-pinned smoke test after the fake-adapter loop satisfies this contract, not to implementation guesswork.
