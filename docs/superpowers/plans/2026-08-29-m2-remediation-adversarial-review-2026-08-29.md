# Adversarial Review — M2 Remediation Implementation Plan

**Plan reviewed:** `docs/superpowers/plans/2026-08-29-m2-remediation.md`
**Review date:** 2026-08-29
**Reviewer:** Codex (GPT-5)
**Repository state:** `9d20cf882cea94d17ec8370dba2309accd32fb98` (`main`, dirty: 27 porcelain entries before this review was written; this review adds one untracked path)
**Verification depth:** read-only static inspection plus Git metadata/evidence inspection — no tests, provider calls, commits, branch changes, or shared-environment mutation

## Verdict

**BLOCKED**

The plan correctly maps every implementation-review finding to work, separates most fixes into bounded commits, and ends with fresh commit-bound evidence. It is not safe to dispatch yet: Task 0's load-bearing baseline precondition is already false, and the ignored-path design still permits an agent to modify or delete a pre-existing ignored file without the fence, reviewer, verifier, or final commit seeing it. Resolve B1–B2 and the major acceptance/evidence defects below before an execution agent receives the plan.

| | Count |
|---|---:|
| Blockers | 2 |
| Major | 6 |
| Minor | 1 |
| Unverified claims | 2 |

## Blocking findings

### B1 — Task 0 cannot identify or reproduce the reviewed baseline

**Where:** plan Task 0, `docs/superpowers/plans/2026-08-29-m2-remediation.md:66-102`; implementation review D1, `docs/superpowers/plans/2026-08-29-m2-implementation-review.md:83-93`

**Failure path:** the implementation review is performed against an uncommitted tree → Task 0 treats a line count as the identity of that tree → the current count is 27, not the required 25, and this companion review makes it 28 → a literal executor must stop immediately. If the executor “fixes” the count or ignores it, `git add -A` can commit a different 25/27/28-path tree, including unrelated files, while still claiming it is the reviewed M2 state.

**Evidence:** read-only inspection returned `HEAD=9d20cf882cea94d17ec8370dba2309accd32fb98`, branch `main`, and 27 `git status --porcelain` entries before this file was created. The two post-review planning artifacts account for the change from the implementation review's 25-path statement, but Git has no committed object proving that the source/test bytes are unchanged since review. `git diff --binary HEAD | /usr/bin/shasum -a 256` returned tracked-diff digest `0f8d2cf2a73ebfd2d96e5c1b1178645d4ee274855facb6acc2a5226e74f01ed8`. Hashing each sorted path from `git ls-files --others --exclude-standard` and then hashing that digest stream returned `452133db4cbe79825be27b26b098dcfea90b2113565889c3ee11462dcaa6e2c6` before this companion review existed; a dispatch-time reproduction must exclude only this exact companion-review path from that second calculation.

**Resolution:** replace count-based admission and `git add -A` with an exact, reviewed baseline manifest: expected `HEAD`, branch, tracked-diff digest, untracked-file paths and digests, and the explicitly allowed review artifacts. Require a human checkpoint before the baseline commit because the original reviewed tree was never committed. Stage only manifest-listed paths. Update Task 0 and A0/A1 below before dispatch.

### B2 — The ignored-path delta misses modification and deletion, and allowed ignored writes remain invisible

**Where:** plan Task 2, `docs/superpowers/plans/2026-08-29-m2-remediation.md:186-271`; current inventory, `src/dev_orchestration/git/repo.py:96-114`; current fence, `src/dev_orchestration/workflow/scope_check.py:11-21`

**Failure path:** an out-of-scope ignored file exists before an agent stage → the agent overwrites or deletes it → `set(repo.ignored_paths()) - set(before)` is empty for overwrite and cannot describe deletion → the fence passes. Separately, the plan explicitly allows an ignored write inside the fence, but keeps it out of `change_inventory`; a run with another visible change can therefore reach review, commit, and `COMPLETE_LOCAL` while leaving the ignored side effect unreviewed and uncommitted.

**Evidence:** the proposed check computes only newly appearing path names at plan lines 249–258. The proposed test at lines 213–218 proves only that an unchanged pre-existing ignored file is not attributed; it never mutates or deletes that file. The current diff and final snapshot are built from `change_inventory`, which intentionally remains blind to ignored paths (`src/dev_orchestration/git/repo.py:96-114`; plan lines 108–110).

**Resolution:** define one content-aware stage mutation inventory covering create, modify, delete, rename, type/symlink change, and ignored paths. Every agent-caused repository mutation must either (a) appear in the exact state reviewed, verified, and committed, or (b) be restored before the next gate. Any out-of-fence mutation escalates. Add overwrite, delete, type-change, and allowed-ignored-write integration tests; do not dispatch on the creation-only delta.

## Major findings

### M1 — Fingerprints are stable only when the reviewer repeats identical wording and line numbers

**Where:** plan Task 11, `docs/superpowers/plans/2026-08-29-m2-remediation.md:1671-1766`; finding model, `src/dev_orchestration/domain/findings.py:26-34`

**Failure path:** remediation inserts lines above an unfixed defect, or the next review says “bounds check missing” instead of “missing bounds check” → the proposed fingerprint's `line` or `summary` changes → the same defect receives a new ID → exhaustion and `unresolved_finding_ids` again refer to a last-round ID rather than the original finding.

**Evidence:** `_fingerprint` hashes `file|line|normalized summary` at plan lines 1741–1750. Every proposed stability test repeats the exact same file, line, and summary; none shifts the line or paraphrases the summary.

**Resolution:** specify finding identity independently of volatile presentation fields, persist match decisions, and fail closed when a round cannot unambiguously match an old finding. Acceptance must cover line drift and minor wording drift as well as two distinct findings in one file.

### M2 — The prompt budget measures packet content, not the argv element that can fail

**Where:** plan Task 10, `docs/superpowers/plans/2026-08-29-m2-remediation.md:1447-1570`; renderer, `src/dev_orchestration/context/packet.py:13-30`; provider join, `src/dev_orchestration/adapters/base.py:49-54`

**Failure path:** packet contents fit the proposed 96,000-byte calculation → rendered labels, paths, separators, truncation notes, and the role prompt are added by `ContextRef.render` and `compose_prompt` → the actual single prompt argument exceeds the asserted budget even though `packet.total_bytes()` passes. `_fit_to_budget` also appends `CONTEXT_NOTES` after sizing, and that category is not added to any stage contract.

**Evidence:** `_size` and `ContextPacket.total_bytes` count only `item.content` (plan lines 1517–1529; `src/dev_orchestration/context/packet.py:30`). `compose_prompt` adds the rendered packet and request prompt later (`src/dev_orchestration/adapters/base.py:49-54`). The new note is appended after fitting at plan lines 1565–1569 and bypasses the allowed-category loop.

**Resolution:** enforce the limit at the adapter-neutral `compose_prompt` boundary over the final UTF-8 prompt argument, count every marker/note/template byte, and declare generated provenance notes in the stage contract. Add adapter argv tests at the exact boundary.

### M3 — The per-task pass counts contradict the plan's own arithmetic and force a false stop

**Where:** plan lines `352-355`, `575-578`, `700-703`, `914-917`, `1082-1085`, `1230-1233`, `1410-1413`, `1607-1610`, `1888-1891`; self-review, `docs/superpowers/plans/2026-08-29-m2-remediation.md:2194`

**Failure path:** baseline 310 + Task 1's three tests + Task 2's five tests = 318 → Task 2 says to expect 312 → the plan's final rule says to stop when the count disagreement exceeds the number added → execution stops despite the intended tests passing.

**Evidence:** the self-review correctly totals 346 before any review-required additions, while the intermediate expectations progress through 312, 315, 318, 320, 322, 326, 329, and 330. These cannot all be cumulative from a 310 baseline and the listed additions.

**Resolution:** remove brittle intermediate totals or regenerate every count from collected tests after each task. Gate on exit code, named tests, unexpected skips/xfails, and a recorded collection manifest; record the final exact count only after all review-required tests exist.

### M4 — Evidence generation leaves an untracked `dist/` tree, so the final clean-tree check fails

**Where:** plan Task 14, `docs/superpowers/plans/2026-08-29-m2-remediation.md:2100-2116`, `:2148-2170`; `.gitignore:1-9`

**Failure path:** A16 removes and rebuilds `dist/` in the repository → `.gitignore` does not ignore `dist/` → Task 14 commits only validation logs and plan documents → `git status --porcelain` is non-empty at the final gate.

**Evidence:** `git check-ignore dist` produced no match. The A16 script writes `dist/*.whl` at plan line 2112, while Step 6 stages only `.superpowers/.../validation/` and `docs/superpowers/plans/` at lines 2153–2154.

**Resolution:** build into a disposable directory outside the repository (preferred), record the exact wheel listing/hash, and remove that explicit temporary directory after evidence capture. Do not use a repository-local destructive cleanup as the acceptance path.

### M5 — The plan lets the implementation executor manufacture its own acceptance evidence

**Where:** plan Task 14, `docs/superpowers/plans/2026-08-29-m2-remediation.md:2038-2170`; repository trust boundary, `../dev-orchestration-spec/AGENTS.md:63-67`

**Failure path:** the same goal-mode executor edits code/tests and writes A1–A17 logs → it can weaken or select tests and then produce internally consistent “passing” artifacts → Task 14 commits those self-produced logs as acceptance evidence → no independent orchestrator has re-run the configured commands against the named commit.

**Evidence:** Task 14 is an ordinary implementation task and contains no role separation or dispatcher/harness gate. The repository contract states that a builder's claim that tests passed is not evidence; the orchestrator must run configured validation and record exit codes.

**Resolution:** route evidence generation to the orchestration/verifier plane after all builder work is committed. Pin checkout SHA before commands, record command/exit code/environment, reject a dirty tree before and after, and have no builder write access to the evidence accepted as the gate.

### M6 — Blank or fallback “criteria” still permit vacuous verification

**Where:** plan Task 4, `docs/superpowers/plans/2026-08-29-m2-remediation.md:659-690`; Task 8, `docs/superpowers/plans/2026-08-29-m2-remediation.md:1194-1223`

**Failure path:** a caller supplies `--criterion ""` or whitespace → the list is non-empty → the runner accepts it and a verifier can PASS the empty string. Alternatively the caller supplies no criterion and the CLI silently promotes an imperative request such as “do it” to an acceptance criterion; exact list equality then proves only that the verifier echoed the same weak text.

**Evidence:** Task 4 checks only list cardinality (`if not acceptance_criteria`), and Task 8 deliberately falls back to `[request]` without normalization or observability validation. No test covers blank, whitespace-only, or duplicate criteria.

**Resolution:** strip and reject blank criteria, reject duplicates after normalization, and distinguish explicit criteria from request-text fallback in the manifest and verification evidence. Preserve the plan's fallback behavior only as an explicit, auditable mode; do not let an empty/whitespace request or criterion become a PASS target.

## Minor findings

- **m1** Task 6 says adding `ProtectedRuleViolation` to the runner will make an attempted protected override `ESCALATED`, but `bootstrap_run` resolves policy before `execute_run` enters its `try` block (`src/dev_orchestration/workflow/runner.py:181-214`, `src/dev_orchestration/workflow/bootstrap.py:151-165`). Canonical configuration says invalid config fails before agent execution, so choose and test one contract: pre-run CLI refusal with no run, or durable `ESCALATED` run; do not claim both.

## Unverified claims

| Claim (plan §) | Why unverifiable | What would settle it |
|---|---|---|
| The current dirty tree still produces `310 passed` (Task 0) | The implementation review records an independent 310-pass run, but this read-only review did not execute tests and the reviewed state has no commit identity. | Independent orchestrator run against the baseline manifest, recording SHA/digests, Python version, exit code, pass/skip count, and clean/dirty state. |
| The 25 source/test paths are byte-for-byte the state independently reviewed | Two plan artifacts were added afterward, but no committed tree or pre-review content manifest exists to prove no source byte changed. | Human confirmation against the current tracked/untracked digests, followed by the exact-path baseline commit and a clean re-run. |

## Assumptions applied

| # | Ambiguity | Assumption applied | Impact if wrong |
|---|---|---|---|
| A1 | The implementation review and remediation plan were created after the 25-path review snapshot. | The original 25-path source/test set is unchanged; the extra pre-review-doc paths are planning artifacts only. Task 0 still requires a human provenance gate because this is not provable from Git. | If any source byte changed after review, the remediation is based on stale evidence and must be re-reviewed. |
| A2 | The plan allows an ignored write inside the scope fence. | An ignored mutation is allowed only if it is represented in the exact review/verification/final-commit state; otherwise it must be restored or refused. | Treating “in scope” as permission to leave invisible side effects would violate the prior D4 contract. |
| A3 | The Python floor change is product-approved remediation rather than an accidental compatibility reduction. | Raising the floor to 3.13 is accepted because the existing fence already uses a 3.13 API (`src/dev_orchestration/scope.py:31-42`; `pyproject.toml:5`). | If 3.11/3.12 support is externally contracted, Task 5 requires a compatible matcher instead and is a protected scope decision. |

---

# End-State Contract

## Definition of done

1. **D1 — Proven baseline:** the remediation branch begins at `9d20cf882cea94d17ec8370dba2309accd32fb98` with one exact-path baseline commit whose manifest identifies every reviewed tracked/untracked input by path and digest; no unrelated dirty path is absorbed.
2. **D2 — Complete mutation attribution:** every repository mutation caused by an implementation or remediation stage—create, modify, delete, rename, mode/type/symlink change, tracked, untracked, or ignored—is either outside-fence and escalated, or inside-fence and present in the exact review/verification/commit state. No invisible residue remains after a terminal run.
3. **D3 — Real local change:** a no-op run cannot create a commit or reach `COMPLETE_LOCAL`; a successful final SHA advances from and descends from base, contains exactly the independently reviewed change inventory, and leaves the worktree clean under the complete mutation definition in D2.
4. **D4 — Bound acceptance:** criteria supplied to verification are non-empty after trimming, unique after normalization, immutable, and identical to the verdict set. The manifest records whether criteria were explicit or request-text fallback. Every criterion is PASS with non-empty evidence before completion.
5. **D5 — Durable finding identity:** the same unresolved defect retains one system ID across reordering, line drift, and minor wording drift; distinct defects never collapse to one ID; ambiguous matches halt rather than guess.
6. **D6 — Real prompt budget:** the final UTF-8 prompt argv element emitted by every adapter is within the declared budget, with render overhead, role template, separators, markers, and truncation notes included. Contract categories are never truncated; truncation is explicit audit evidence.
7. **D7 — Configuration/tier truthfulness:** protected override attempts are visibly refused under one documented pre-run/run-state contract; role bindings resolve framework < global < project; worktree/binary overrides are loaded; tier overrides raise controls and never lower classification/profile minimums.
8. **D8 — Commit-bound independent evidence:** all prior A1–A17 checks plus this review's checks pass against one committed code SHA. Evidence is generated by the orchestrator/verifier plane from a clean checkout, records environment/command/exit code, and does not rely on builder-authored pass claims.

## Invariants — must hold throughout, not just at the end

1. The original dirty M2 tree is not stashed, reset, force-checked-out, or partially staged; Task 0 stops on any baseline-manifest mismatch.
2. No task pushes, merges, deploys, publishes, mutates production data, or changes the protected authorization/security model.
3. Provider invocations remain argument arrays; no `shell=True` or interpolated command string is introduced.
4. Existing and newly added tests are not deleted, skipped, weakened, narrowed, or replaced with assertions on mocks that no longer exercise the production join.
5. The three specifically protected tests named by the plan remain unchanged in strength, and the broader prohibition in I4 applies to all acceptance tests.
6. Every builder task ends at a local commit only after targeted tests and quality gates pass; acceptance evidence is generated later and independently.
7. Plan versions and approved artifacts remain append-only/immutable.

## Prohibitions — out of bounds regardless of outcome

1. Do not bypass Task 0 by editing the expected path count or deleting a dirty file until the count matches.
2. Do not use `git add -A` for the recovery baseline; stage only manifest-listed paths.
3. Do not treat “path existed before” as proof that an ignored file was not modified.
4. Do not allow an ignored mutation to disappear from review, verification, execution summary, or final state merely because the fence permits its path.
5. Do not hardcode finding IDs, expected test counts, criteria, PASS verdicts, prompt sizes, or changed-file lists in production code.
6. Do not make prompt-size tests pass by raising the budget above the provider/OS-safe ceiling or by silently dropping contract content.
7. Do not delete, skip, weaken, or rename away a failing acceptance test to obtain green evidence.
8. Do not generate accepted A1–A17 evidence in the same builder role/session that changed the implementation.
9. Do not build wheel evidence into repository-local `dist/`; use an explicit disposable output directory.

## Stop conditions — halt and escalate

1. Pre-baseline `HEAD`, branch, tracked-diff digest, untracked manifest/digest, or allowed path set differs from the reviewed manifest.
2. The independent baseline suite does not reproduce the recorded baseline result, or source/test content changed after the implementation review.
3. Complete ignored-state attribution would require silently skipping an unreadable, special, or unbounded path; record it and escalate rather than assume unchanged.
4. A prior finding cannot be matched to the new review round unambiguously.
5. A non-truncatable contract alone exceeds the final prompt budget.
6. A targeted test fails for behavior outside this remediation, an unexpected skip/xfail appears, or the collected test set shrinks.
7. Evidence checkout becomes dirty, HEAD moves, or any log names a different code SHA/environment than the one tested.
8. Any requested fix would require changing Python compatibility, scope-fence semantics, protected rules, or another approved architecture boundary beyond the assumptions above.

## Scope boundaries

**In scope:** paths enumerated by the plan's File Structure, tests required by this companion contract, `pyproject.toml`, the three M2 plan/review documents, this companion review, and commit-bound M2 acceptance evidence.

**Out of scope / do not touch:** canonical design documents in `../dev-orchestration-spec`, unrelated M1 history, provider CLI installations, user/global configuration files, remote branches, deployment/publication state, production data, and unrelated dirty paths.

## Acceptance checks

| # | Check | Command / observation | Pass condition | Evidence artifact |
|---|---|---|---|---|
| A0 | Baseline provenance | Before Task 0: `git rev-parse HEAD`; `git branch --show-current`; tracked/untracked digest commands recorded in B1 | HEAD is `9d20cf8…`, branch is `main`, exact manifest/digests match, and a human approves the baseline commit inputs. | `validation/A0-baseline.log` + baseline manifest |
| A1 | Baseline commit exactness | `git diff-tree --no-commit-id --name-only -r <baseline-sha>` and `git status --porcelain` | Commit contains exactly manifest-listed paths; status is empty; no `git add -A` side effects. | `validation/A1-baseline-commit.log` |
| A2 | Ignored mutations cannot escape | `.venv/bin/python -m pytest -q tests/workflow/test_scope_check.py tests/workflow/test_runner.py -k 'ignored and (modified or deleted or type_change or in_scope)'` | Exit 0; pre-existing overwrite/delete/type change escalates when out of scope, and an in-scope ignored mutation is reviewed+committed or refused/restored. | `validation/A2-ignored-mutations.log` |
| A3 | Final snapshot is exact and non-empty | `.venv/bin/python -m pytest -q tests/git/test_repo.py tests/workflow/test_runner.py -k 'commit_snapshot or changes_nothing or clean_final_commit'` | Exit 0; no-op fails; deletion/rename work; only reviewed paths commit; complete final state is clean. | `validation/A3-final-snapshot.log` |
| A4 | Criteria are non-vacuous and bound | `.venv/bin/python -m pytest -q tests/workflow/test_runner.py tests/test_cli_run.py -k 'criteria or criterion'` | Exit 0; empty list, blank/whitespace, duplicates, dropped criteria, and swapped criteria cannot complete; request fallback is explicitly persisted and evidenced. | `validation/A4-criteria.log` |
| A5 | Finding identity survives realistic drift | `.venv/bin/python -m pytest -q tests/workflow/test_stages.py -k 'finding and (round or line or wording or distinct)'` | Exit 0; same defect keeps ID after line/wording drift; distinct defects remain distinct; ambiguity escalates. | `validation/A5-finding-identity.log` |
| A6 | Emitted prompt is actually bounded | `.venv/bin/python -m pytest -q tests/context/test_assembler.py tests/adapters/test_context_rendering.py -k 'budget or oversized or truncat'` | Exit 0; `len(compose_prompt(request).encode('utf-8'))` is within the declared limit for every adapter path; notes are counted and contract content is never truncated. | `validation/A6-prompt-budget.log` |
| A7 | Config and tier joins | `.venv/bin/python -m pytest -q tests/config tests/adapters/test_registry.py tests/workflow/test_runner.py tests/test_cli_run.py -k 'protected or override or role or global_config or tier'` | Exit 0; precedence, protected refusal, persisted overrides, and no-downgrade behavior are all covered. | `validation/A7-config-tier.log` |
| A8 | Schemas are durable and no temp leak remains | `.venv/bin/python -m pytest -q tests/workflow/test_invoke.py tests/test_schemas.py` | Exit 0; both attempts use a schema under the run store; no structured invocation creates an unmanaged temp tree. | `validation/A8-schemas.log` |
| A9 | Full regression and collection integrity | `.venv/bin/python -m pytest -q`; `.venv/bin/python -m pytest --collect-only -q` | Both exit 0; exact pass/skip and collected-node counts are recorded; no unexpected skip/xfail and no shrink from the post-remediation collection manifest. | `validation/A9-pytest.log` + node manifest |
| A10 | Static gates | `.venv/bin/ruff check . && .venv/bin/ruff format --check src tests` | Both exit 0 against the evidence SHA. | `validation/A10-ruff.log` |
| A11 | Wheel/CLI without repo residue | `.venv/bin/dev-orch --help`; `.venv/bin/python -m build --wheel --outdir <fresh-temp-dir>`; inspect wheel; `git status --porcelain` | Commands exit 0; expected commands and all seven role templates exist; repository status stays empty. | `validation/A11-cli-wheel.log` + wheel SHA/listing |
| A12 | Protected operations remain absent | `.venv/bin/python -m pytest -q tests/git/test_guards.py` | Exit 0, including runtime-argv and planted-literal guards. | `validation/A12-guards.log` |
| A13 | Independent provenance | Inspect every acceptance log header and the dispatcher record | One orchestrator/verifier run produced all logs from one clean committed code SHA; code SHA is an ancestor of the evidence commit; no builder claim is used as a gate. | `validation/README.md` + dispatcher record |

All original A1–A17 acceptance checks remain required. The table above adds or strengthens checks; it does not replace the prior contract.

## Recovery position

Before Task 0, the last safe position is the current dirty tree at `9d20cf8`; preserve it byte-for-byte and stop on any provenance mismatch. After the exact baseline commit, every workstream returns to the last passing local commit with a normal revert commit if recovery is needed—never reset, stash, force-checkout, or delete unknown work. Test/build artifacts use explicit temporary directories outside the repository. Failed run worktrees and audit artifacts remain intact until their evidence is captured; cleanup is a separate approved action.

---

# Execution Routing

| # | Workstream | Owns (paths) | Depends on | Complexity | Harness fit | Human gate |
|---|---|---|---|---|---|---|
| W0 | Baseline recovery and provenance | current dirty paths, baseline manifest, Task 0 commit only | none | High-judgment | Interactive local Git harness | **Yes — before staging/commit** |
| W1 | Complete mutation inventory and final snapshot | `git/repo.py`, `workflow/scope_check.py`, relevant Git/runner tests | W0 | High-judgment | Sandbox with temporary Git repos and ignored/symlink fixtures | Yes — review D2 semantics before runner join |
| W2 | Terminal gates, criteria, tier/config | `workflow/runner.py`, `workflow/tiers.py`, `config/`, `cli.py`, registry and tests | W0; W1 interface | High-judgment | Local fake-adapter integration | No; independent verification at completion |
| W3 | Prompt budget, finding identity, durable schemas | `context/`, `workflow/stages.py`, `workflow/invoke.py`, `artifacts/store.py`, tests | W0 | High-judgment | Local harness; byte-accurate adapter argv inspection | Yes — approve finding-match rule before implementation |
| W4 | Dead-code cleanup and acceptance-test strengthening | files in Tasks 12–13 | W1–W3 | Moderate | Either | No |
| W5 | Independent acceptance and evidence commit | validation logs/README, wheel temp output, plan docs | W1–W4 committed | Moderate | Orchestrator/verifier harness, not builder session | **Yes — accept evidence before evidence commit** |

**Ordering and parallelism**

W0 is serial and blocks everything. After its human gate, W1 and W3 may proceed in parallel because their production paths do not overlap; W2 must consume W1's mutation-inventory interface and must not edit `runner.py` concurrently with W1's runner integration. W4 follows the implementation joins. W5 runs last from a clean committed SHA and must be independent of all builder workstreams.

**Per-workstream done-checks**

| Workstream | Closes | Evidence to produce |
|---|---|---|
| W0 | D1; A0–A1 | Baseline manifest, digest checks, human approval record, exact commit listing |
| W1 | D2–D3; A2–A3 | Ignored overwrite/delete/type fixtures, exact final snapshot evidence |
| W2 | D4, D7; A4, A7 | Criteria, protected-config, role precedence, and tier manifests/tests |
| W3 | D5–D6; A5–A6, A8 | Stable-ID index/match evidence, final prompt byte measurements, durable schemas |
| W4 | I4–I7; A9–A10, A12 | Full collection manifest, regression and static-gate results |
| W5 | D8; A9–A13 plus prior A1–A17 | One commit-bound independent evidence set and clean final status |

---

## Open questions

No dispatch-blocking design questions remain. The baseline's historical identity is an evidence gap, not an implementation choice; W0's human provenance gate must settle it before execution.
