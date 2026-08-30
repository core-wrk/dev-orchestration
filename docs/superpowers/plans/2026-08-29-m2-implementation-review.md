# Implementation Review — M2 — The Loop

**Reviewed:** M2 implementation in the working tree of `dev-orchestration`
**Review date:** 2026-08-29
**Evidence audited:** `.superpowers/sdd/2026-08-29-m2-the-loop/validation/A1.log` – `A17-guards.log`
**Acceptance contract:** `docs/superpowers/plans/2026-08-29-m2-the-loop-adversarial-review-2026-08-29.md` (checks A1–A17)
**Verification depth:** all 17 acceptance checks re-run independently; every finding below reproduced by direct execution against the code, not by inspection alone

## Verdict

**BLOCKED**

The suite is real — 310 tests pass, `ruff check` and `ruff format --check` are clean, and the wheel ships all seven role templates. The architecture is sound: stage contracts isolate roles, validation is structurally independent of builder narration, and plan approval is immutable. But four defects let a run certify itself falsely or crash on a real repository, and the recorded evidence names a commit that contains none of the implementation.

| | Count |
|---|---:|
| Blockers | 4 |
| Major | 7 |
| Minor | 4 |

## Blocking findings

### B1 — A run that changes nothing completes with a fabricated empty commit

**Where:** `src/dev_orchestration/git/repo.py:137-142`, `src/dev_orchestration/workflow/runner.py:519-524`

`commit_snapshot` discards its `paths` argument (`del paths`), stages with `git add -A`, and commits with `--allow-empty`. The runner reaches it via `if changed or final_commit == base_commit`, so the branch is taken precisely when there is nothing to commit.

**Reproduced** with an implementation worker that writes nothing:

```
state: COMPLETE_LOCAL
git diff --stat base..final  →  (EMPTY DIFF)
execution-summary.json       →  "changed_files": []
```

Acceptance check A5 requires the final SHA to "contain the reviewed diff". It contains nothing. `tests/workflow/test_runner.py:132` encodes this as expected behaviour, so the suite actively defends the defect.

### B2 — The verifier can certify a run by judging nothing

**Where:** `src/dev_orchestration/domain/findings.py:86-97`, `src/dev_orchestration/workflow/runner.py:508`

`Verification.every_criterion_has_a_verdict` checks only that the provider's payload is internally consistent — that its `verdicts` match its own `criteria`. Nothing binds `verification.criteria` to the run's actual `acceptance_criteria`. A provider returning `criteria: []`, `verdicts: []`, `outcome: PASS` validates, and the runner's `all(...)` over an empty list is `True`.

**Reproduced:**

```
criteria supplied to verifier: ['the flag exists']
verifier returned:             criteria=[] verdicts=[] outcome=PASS
final state:                   COMPLETE_LOCAL
```

A2 exercised FAIL and UNKNOWN verdicts, so this path was never covered.

### B3 — Gitignored paths escape the scope fence entirely

**Where:** `src/dev_orchestration/git/repo.py:96-101`

`change_inventory` is `git diff --name-status` (tracked paths only) unioned with `status_paths()` (`git status --porcelain`, which omits ignored files). Both are blind to anything matched by `.gitignore`, so the fence, the implementation-review diff, and `git add -A` all miss it.

**Reproduced** in a repository whose `.gitignore` contains `dist/`, with a worker that writes `dist/payload.sh`:

```
fence saw:                        []
state:                            COMPLETE_LOCAL, reason: (none)
file still on disk in worktree:   True
```

M1 closed three fail-open holes in this fence. This is a fourth, in the milestone that gives the fence its first production caller. Real repositories ignore `dist/`, `build/`, `node_modules/`, `.env`, and `*.log`.

### B4 — The scope fence crashes on every declared-supported Python below 3.13

**Where:** `src/dev_orchestration/scope.py:42`, `pyproject.toml:5`

`_matches` calls `PurePosixPath.full_match`, added in CPython 3.13. `pyproject.toml` declares `requires-python = ">=3.11"`. On 3.11 or 3.12 any fence pattern containing `*`, `?`, or `[` raises `AttributeError` inside the safety control, which the runner catches as an unexpected failure. The local environment is 3.14.6, so no test can see it.

## Major findings

### D1 — The recorded evidence names a commit that contains none of the implementation

Every `A*.log` records `commit: 9d20cf882cea94d17ec8370dba2309accd32fb98`.

```
git ls-tree -r --name-only 9d20cf8 | grep -c "workflow/\|context/\|roles/"  →  0
```

The entire M2 implementation is uncommitted working-tree state across 25 paths. The `README.md` discloses this in prose, but the per-log `commit:` field attributes results to a tree that cannot produce them, and nobody can reproduce the evidence from the named SHA. The plan specified per-task atomic commits; none were made.

### D2 — The whole context packet is passed as a single argv element

**Where:** `src/dev_orchestration/adapters/base.py:49-55`, `claude.py:71`, `codex.py:131`

`compose_prompt` concatenates every context item — including the complete `git diff` — into one command-line argument. Linux caps a single argv element at `MAX_ARG_STRLEN` (128 KiB); this machine's `ARG_MAX` is 1048576. `ContextPacket.total_bytes()` exists but is referenced only by a test, and no budget is enforced anywhere. Fake adapters never surface this; a real run against a moderate diff fails with `E2BIG`.

### D3 — Project and global configuration are inert

**Where:** `src/dev_orchestration/adapters/registry.py:94-99`, `src/dev_orchestration/cli.py:130-136`

`default_registry` accepts `project_config` and immediately does `del project_config`. `GlobalConfig` is never loaded from disk anywhere in `src/`. Consequently role-to-adapter bindings, model aliases, `codex_binary`, and `worktree_root` are unreachable configuration; the CLI hardcodes the worktree path. `ProjectConfig` has no `roles` field at all, so a repository cannot express a binding even in principle.

### D4 — The protected-rule guard cannot fire

**Where:** `src/dev_orchestration/config/resolver.py:42`

`resolve_policy` ends with an unconditional `resolved.update(PROTECTED_DEFAULTS)`, so the checks at `bootstrap.py:161` and `runner.py:235` can never trip and `ProtectedRuleViolation` is unreachable.

**Reproduced:** a layer setting `protected.autonomous_push: true` resolves to `False` with no refusal, no event, and no escalation. The rule holds, but an attempted override is silently swallowed rather than reported.

### D5 — Finding IDs are renumbered on every review round

**Where:** `src/dev_orchestration/workflow/stages.py:130-137`

`_renumber` assigns a fresh sequential ID to every finding on every review. The same unfixed defect walks `F001 → F002 → F003` across remediation cycles.

**Reproduced:** the exhaustion message reads `blocking findings ['F003'] survived 2 remediation cycles`, naming an ID that first appeared in the final round. `unresolved_finding_ids` therefore cannot reference a finding across rounds, which is the stated reason IDs are system-assigned rather than provider-supplied.

### D6 — A tier override may silently downgrade below the classifier's tier

**Where:** `src/dev_orchestration/workflow/runner.py:170-178`

`_effective_tier` raises the tier to a profile minimum but applies any override otherwise. `--tier trivial` against a `standard` classification skips planning and plan review entirely in any repository without profile minimums. A9 tested only that an override cannot beat a minimum; `tests/test_cli_run.py:204-208` asserts the downgrade succeeds.

### D7 — There is no way to state acceptance criteria

**Where:** `src/dev_orchestration/cli.py:137`

`run` passes `acceptance_criteria=[request]`. Every real run asks the verifier "did you do &lt;the request text&gt;?" as its single criterion, and the CLI exposes no option to supply anything better.

## Minor findings

### E1 — Dead code that reads as enforcement

- `runner._persist_context` (`runner.py:124-139`) is referenced nowhere.
- `assemble`'s `fence` parameter (`assembler.py:84-86`) is `del`'d and never passed by any caller.
- `DEFAULT_ROLES["goal_executor"]` (`registry.py:21`) is bound to no stage.
- The runner's `cycle > MAX_REMEDIATION_CYCLES + 1` bound (`runner.py:478`) is unreachable; the real budget is enforced in `stages.remediate`.

### E2 — A temporary directory leaks per structured invocation

`invoke_structured` (`invoke.py:33-35`) calls `tempfile.mkdtemp()` on every structured call and never removes it. The generated schema is also absent from the run's audit artifacts.

### E3 — Two acceptance checks assert less than their pass condition

- A13's test is named `test_success_and_escalation_leave_complete_audit_artifacts` but exercises only the success path, and asserts `.is_file()` rather than artifact validity or the "references one base/final code state" condition it is cited for.
- A10's pass condition includes "approval is immutable"; the test never attempts a second approval.

### E4 — The final commit is not restricted to the reviewed change set

`commit_snapshot` stages with `git add -A` rather than the inventory the implementation reviewer and verifier actually saw.

## What is sound and must not regress

- The read-only role/adapter join check (`registry.py:60-84`) fails closed when a restriction cannot be honoured, with a comment correctly explaining why neither a registry test nor an adapter test can catch the gap.
- `ReviewResult.pass_cannot_carry_blocking_findings` and `Classification.tier_is_implemented` are real invariants enforced at the model boundary.
- Validation is structurally independent of builder narration: `Category.BUILDER_NARRATION` appears in no stage contract, and a test asserts it cannot reach a reviewer.
- Plan versions and the approved plan are immutable through exclusive-create (`store.py:88-92`, `103-124`).
- A4's fence coverage across untracked, staged, and unstaged states is genuine and correctly parameterised.
- A16 reproduced independently: the wheel contains all seven `roles/*.md` templates.
