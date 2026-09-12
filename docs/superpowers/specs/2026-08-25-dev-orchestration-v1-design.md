# dev-orchestration V1 — Design

**Date:** 2026-08-25
**Status:** Approved for implementation planning
**Source specification:** `/Users/andrewodonnell/GitRepos/dev-orchestration-spec` (13 documents)
**Pilot:** `helmfast-OS`, `helmfast-app`, `helmfast-site`

---

## 1. Purpose of this document

The source specification describes a complete V1 across 17 phases. This design records
what the **first build** delivers, which specification assumptions did not survive contact
with the actual machine, and which decisions were made deliberately rather than inherited.

Where this document and the source specification disagree, **this document wins** — every
divergence below is deliberate and is justified by evidence gathered on 2026-08-25.

---

## 2. Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| Pilot repos | `helmfast-OS`, `helmfast-app`, `helmfast-site` | `helmfast-en2` excluded — no code to validate against |
| Build scope | Thin vertical slice, three milestones | Prove the loop on real code before backfilling tiers |
| Executor | `codex exec`, Goal Mode as a detected capability | Goal Mode is not drivable headlessly (§3.2) |
| Framework home | `~/GitRepos/dev-orchestration` (new repo) | Spec names it; keeps the spec package pristine |
| Harness | Standalone Python CLI, no Claude Code skill wrapper | Vendor-neutral, testable without a harness |
| Run artifacts | Committed in each repo's `.ai/runs/` | Audit trail travels with the code it explains |
| First real run | `helmfast-OS` | Highest value; trivial-tier run precedes standard-tier |
| Dirty working trees | Hard block, no escape hatch | Framework never touches uncommitted work |
| Business-knowledge trees | Hard-fenced out of scope | A development framework has no business editing GTM docs |
| GSD relationship | **Decided** 2026-09-01 — GSD owns intent, `dev-orch` owns execution | ADR-002; see §11.1 |

---

## 3. Environment findings

These were verified on the machine on 2026-08-25, not assumed.

### 3.1 The pilot target is four repos, not one

`/Users/andrewodonnell/GitRepos/helmfast` is an umbrella directory, not a repository. It
contains four independent git repos plus two empty directories (`planning/`, `.claude/`).
The specification's `helmfast-os` corresponds to only one of them.

| Repo | Contents | Stack | Tree state |
|---|---|---|---|
| `helmfast-OS` | Business knowledge + a live Python pipeline | Python 3, pytest, ruff, no `pyproject.toml` | **Dirty** — 10 modified, 2 untracked dirs, 1 active worktree |
| `helmfast-app` | Docs only — `docs/product/`, `docs/architecture/` | none | Clean |
| `helmfast-site` | Marketing site | Astro + Cloudflare Wrangler | Clean |
| `helmfast-en2` | Docs/config | none | **Excluded from pilot** |

### 3.2 Codex Goal Mode is not drivable headlessly

The `codex` binary is **not on `PATH`**. It ships inside the ChatGPT desktop app at
`/Applications/ChatGPT.app/Contents/Resources/codex`, reporting `codex-cli 0.146.0-alpha.9.2`.

Probing that binary:

- `codex exec` runs headlessly and supports `--json`, `--output-schema <FILE>`, `-m <MODEL>`,
  `-C <DIR>`, `-s <SANDBOX_MODE>`, and `-o/--output-last-message <FILE>`.
- `codex features list` reports `goals` as **stable**, and both `gpt-5.6-sol` and
  `gpt-5.6-luna` are available models.
- Goal Mode surfaces only as an interactive `/goal` slash command. There is **no** `--goal`
  flag and **no** `goal` subcommand on `exec`.

Consequence: the specification's default execution strategy cannot run unattended today.
The specification's own fallback (`CLI-AND-ADAPTERS.md` §5) applies, and the design treats
`goal` as a capability to detect rather than a contract to depend on.

### 3.3 A deployment command is one config typo away from production

`helmfast-site` ships `npm run deploy` → `wrangler deploy`. Nothing in the source
specification prevents that from being registered as a validation command, at which point
the orchestrator would deploy to production believing it was running a build. See §5.4.

### 3.4 helmfast-OS forbids the specification's documentation migration

`helmfast-OS/CLAUDE.md` states that `pipeline/runtime/prompts.py` hardcodes `marketing/`,
`operations/`, and `market-research/` as template-loading roots, and that moving them
breaks the live pipeline and its tests. Independently of that constraint, those trees are
business knowledge and are out of scope for a development framework entirely. See §6.

---

## 4. Architecture

```text
~/GitRepos/dev-orchestration/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── docs/
│   ├── specification/            # the 13 source docs, copied and frozen
│   └── superpowers/specs/        # this document
├── src/dev_orchestration/
│   ├── cli.py                    # Typer entry point → `dev-orch`
│   ├── config/
│   │   ├── models.py             # Pydantic: GlobalConfig, ProjectConfig, ResolvedConfig
│   │   ├── resolver.py           # six-layer precedence
│   │   └── loader.py
│   ├── domain/
│   │   ├── enums.py              # RunState, Outcome, Tier, ProjectClass
│   │   ├── run.py                # Manifest, Event
│   │   └── findings.py           # Finding, ReviewResult, Classification, Verification
│   ├── git/
│   │   ├── repo.py               # discovery, clean guard, base commit, branch, commit
│   │   ├── worktree.py
│   │   └── guards.py             # push / merge / deploy fail closed
│   ├── scope.py                  # the hard fence
│   ├── artifacts/
│   │   ├── store.py              # .ai/runs/<run-id>/, append-only plan versions
│   │   └── events.py             # events.jsonl
│   ├── adapters/
│   │   ├── base.py               # AgentAdapter protocol, AgentRequest / AgentResult
│   │   ├── codex.py              # codex exec, binary discovery, goal detection
│   │   ├── claude.py             # claude -p --output-format json
│   │   └── registry.py           # logical role → adapter + model alias
│   ├── workflow/
│   │   ├── engine.py             # state machine, transition enforcement
│   │   ├── classify.py
│   │   ├── tiers.py              # trivial and standard tier paths
│   │   └── validation.py         # subprocess runner
│   ├── roles/                    # prompts as template files, never Python string literals
│   └── doctor.py
├── profiles/
│   ├── classes/                  # internal_operating_system, client_application, marketing_website
│   └── risks/                    # agent_orchestration, canonical_knowledge, external_api, customer_data
├── schemas/                      # JSON Schema, also fed to `codex --output-schema`
├── templates/
└── tests/
```

The workflow engine never sees provider command syntax. Roles resolve through
`adapters/registry.py` to an adapter plus a model alias, exactly as the specification
requires.

---

## 5. Divergences from the source specification

### 5.1 Execution strategy is `exec`, not `goal`

`execution.strategy` remains the abstract interface the specification defines. `exec` is
implemented and becomes the V1 default. `goal` remains a declared strategy whose
availability is probed at `doctor` time and reported honestly as unavailable-headless.

No invented CLI syntax. The adapter never guesses at a `/goal` invocation.

### 5.2 Codex binary discovery is three-tier

Resolution order: `$PATH` → `/Applications/ChatGPT.app/Contents/Resources/codex` →
explicit config override. The resolved absolute path **and** the reported version are
written into every run manifest, because the installed build is an alpha whose contract
may move under us.

### 5.3 Structured output is enforced at the provider

`codex exec --output-schema <FILE>` applies our JSON Schema at the provider rather than
parsing hopefully afterwards. Used for classification, plan review, implementation review,
and final verification. The Claude adapter uses `-p --output-format json` with tools
restricted to read-only for review roles. Both paths validate against the same Pydantic
models; schema failure triggers exactly one bounded retry, then escalates.

### 5.4 Validation commands are screened (new)

The source specification has no equivalent control. Configured validation commands are
screened at config-load time against a deny-list — `deploy`, `wrangler`, `publish`,
`push`, `merge`, `release` — and a match **fails closed** before any agent runs. This is
not overridable by repository configuration; it sits alongside the specification's
`protected:` rules.

### 5.5 The scope fence (new)

See §6. The source specification assumes a whole repository is in scope. That is wrong for
`helmfast-OS`, and likely wrong for any operating-system repo that mixes business knowledge
with code.

---

## 6. The scope fence

`.ai/project.yaml` gains a `scope` block declaring `include` and `exclude` path globs.
The fence is enforced independently of what agents are merely *told* about it:

1. **Post-execution diff check** — the run's diff is compared against the fence. Any file
   touched outside `include` fails the run, regardless of what the agent reported.
2. **`doctor`** — prints the active fence, so it is never a silent policy.

The fence governs what agents may **modify**. It does not govern what repository code may
**reference**: `pipeline/runtime/prompts.py` legitimately reads three excluded trees as
template roots, and that continues to work.

Prompt-source selection is a separate context-assembly control. The orchestrator supplies
a repository file only when the resolved `context` configuration or an active profile
explicitly selects it; a selected file must resolve to a regular file within the repository
root and pass the role contract and budget. A modification-scope match never makes a file
prompt-eligible by itself, and a declared context source is not rejected merely because it
is excluded from modification. This concerns context the orchestrator supplies, not an
independent provider action inside the worktree.

---

## 7. Git safety

- Clean-tree guard blocks non-trivial runs and names the offending files. There is **no
  `--allow-dirty` flag**, deliberately: bypassing it would branch from `HEAD` and silently
  exclude in-progress work.
- Push, merge, and deploy have **no code path** — not a flag defaulting to off, not a
  function guarded by a conditional. `git/guards.py` exists to make the absence explicit
  and to fail closed if called.
- `codex exec` runs with `-s workspace-write` and `cwd` pinned to the run worktree.
  `--dangerously-bypass-approvals-and-sandbox` never appears in the codebase.
- Branches follow `ai/<run-id>-<slug>`; worktrees live under
  `~/.dev-orchestration/worktrees/<repo>/<run-id>/`.

---

## 8. Milestones

### M1 — Foundation and onboarding

**Build:** config models and six-layer resolver; domain enums and manifest; git repo/worktree
handling and guards; scope fence; artifact store with append-only plan versioning and
`events.jsonl`; both provider adapters with capability detection; `dev-orch init`;
`dev-orch doctor`.

**Acceptance:**
- [ ] `dev-orch --help` and `dev-orch doctor` succeed on macOS.
- [ ] Invalid configuration fails before any agent is invoked.
- [ ] A validation command matching the deny-list fails config load.
- [ ] Stricter profile policy beats weaker class policy.
- [ ] Dirty repository blocks a non-trivial run and names the files.
- [ ] Worktree creation, local commit, and cleanup all work.
- [ ] No push/merge/deploy code path exists (asserted by test).
- [ ] `doctor` reports Codex path, version, and goal-capability state.
- [ ] All three pilot repos carry `AGENTS.md`, `.ai/project.yaml`, `.ai/context.md`.
- [ ] Existing `helmfast-OS/CLAUDE.md` reduced to a thin pointer; no existing file moved.

### M2 — The loop

**Build:** classifier; plan → plan-review → reconcile with immutable versioning; external
plan import; `codex exec` executor; validation runner; implementation review; remediation
(max 2 cycles); final verification; `status` and `runs`. `trivial` and `standard` tiers only.

**Acceptance:**
- [ ] `plan-v1.md` persists before review; `plan-v2.md` never overwrites it.
- [ ] Review findings carry stable IDs; remediation references them.
- [ ] An imported external plan is copied and hashed, with `plan_origin: external`.
- [ ] Required validation commands run independently of any builder self-report.
- [ ] A file touched outside the scope fence fails the run.
- [ ] Remediation stops at 2 cycles and escalates.
- [ ] Every acceptance criterion receives PASS / FAIL / UNKNOWN.
- [ ] `status` shows current stage and next action without raw JSON.
- [ ] A trivial-tier change skips planning and plan review entirely.

### M3 — Pilot

**Build:** nothing new. Run the framework.

Run 2026-09-01 on `helmfast-OS`: twelve runs, three reaching `COMPLETE_LOCAL`.

**Acceptance:**
- [x] One trivial-tier run completes on `helmfast-OS`. — two did.
- [x] One standard-tier run completes on `helmfast-OS`. — one did.
- [x] No remote push occurs. — pilot evidence is still local and unpushed.
- [x] Run artifacts are legible to a future investigator without chat history. — the
      manifests, classifications and review rounds carried this decision without
      recourse to any transcript.
- [x] At least one framework improvement is identified from pilot evidence. — the
      scope-fence ephemeral-cache fix (`04ef81c`), plus the cycle-finalization close
      barrier and prompt supersession; adapter reliability (below) is a fourth,
      identified on review of the pilot record 2026-09-12.
- [x] The GSD boundary decision (§11.1) is made on evidence. — decided 2026-09-01,
      recorded in `M3-PILOT-REPORT.md`, promoted to ADR-002 on 2026-09-12.

**The finding that should shape M4.** A third of pilot runs failed inside the Codex
adapter rather than anywhere in the workflow: two provider exits, a Classification
schema miss with four validation errors, a ReviewResult miss with twenty-four. One
further run is still parked in `EXECUTING` with no timeout to recover it. Process
design is not the binding constraint on completion rate; adapter and
structured-output reliability is.

---

## 9. Repository onboarding contracts

| | `helmfast-OS` | `helmfast-app` | `helmfast-site` |
|---|---|---|---|
| Class | `internal_operating_system` | `client_application` | `marketing_website` |
| Profiles available | agent_orchestration, canonical_knowledge, external_api, customer_data | — | — |
| Scope include | `pipeline/`, `tests/`, `planning/`, `README.md` | `docs/` | `src/`, `docs/`, `scripts/`, `public/`, config files |
| Scope exclude | `marketing/`, `operations/`, `market-research/`, `strategy/`, `context/`, `data-templates/`, `var/`, `.venv/` | — | `node_modules/`, `dist/`, `.astro/`, `.wrangler/` |
| Validation | `pytest`, `ruff check` | none — docs only | `npm run build` |
| Deploy | — | — | **denied**; `wrangler deploy` on deny-list |

Every validation command is **proven to run in the repository** before it becomes required
policy, per `HELMFAST-OS-PILOT.md` §7. Onboarding is additive: new files only, no moves,
no edits to existing content beyond reducing `CLAUDE.md` to a pointer.

---

## 10. Testing

Test-driven throughout.

**Unit:** config precedence; scope fence matching; validation deny-list; state transitions;
plan-version immutability; worktree path generation; push prohibition; artifact
serialization; structured-output validation; remediation cycle cap.

**Integration:** the whole loop runs against **fake adapter binaries** — shell scripts on
`PATH` emitting canned JSON — so the state machine is exercised without burning tokens or
depending on provider availability.

**Live:** provider smoke tests are marked and skipped by default.

---

## 10a. Amendment — context minimization (2026-08-26)

`CONTEXT-MINIMIZATION-AUDIT.md` was approved after this design was written and
applied across all 13 specification files on branch `audit/context-minimization`
(spec package commit `8c1c3d6`). Its governing principle now binds this build:

> **Encode invariants, not intelligence.**

### What changed for M1

Most of M1 is unaffected — it builds configuration, git isolation, artifacts and
adapters, none of which assemble prompts. Three things did change:

- **Task 14** now also extends `ProjectConfig` with a `ContextPolicy` block
  (`CONFIGURATION.md` §6), and its generated `AGENTS.md` template was rewritten
  to the six-question contract in `DOCUMENTATION-STANDARD.md` §4. Three new
  tests enforce it, including one asserting no generic engineering advice leaks
  into a generated file and one holding it within the persistent byte budget.
- **Task 15** gained an agent-surface inventory step, recording what each pilot
  repo already tells its agents — and its size — before onboarding reduces it.
- **The plan's Global Constraints** carry the principle, so every remaining
  implementer receives it.

Tasks 1–11 were reviewed against the directive and need no rework. The one
forward-looking interface change — `AgentRequest.context: ContextPacket | None`
— is additive and lands with the assembler, not now.

### What deferred to M2, and why

The context assembler itself is **Phase 5** of the specification's build
sequence, which sits after adapters and before the workflow loop. That is M2
territory: M1 has no stage that constructs a prompt, so building an assembler now
would produce an untestable component with no caller.

The audit's own §13 warns against turning minimization into an over-engineered
subsystem. Deterministic path and category selection is the V1 mechanism; vector
stores, embeddings and retrieval scoring are explicitly excluded until pilot
evidence justifies them.

### Two spec drifts closed

The audit surfaced two controls that existed in **code but not in the
specification** — the validation-command deny-list and the scope fence, both
added during M1 as rulings. Both are now written into `CONFIGURATION.md`. The
specification and the implementation agree again.

---

## 11. Open decisions

### 11.1 GSD boundary — DECIDED 2026-09-01

**Decision: GSD owns intent and planning state; `dev-orch` owns execution.** A GSD
plan enters only by explicit import as an external approved plan
(`plan_origin: external`), copied and hashed by the framework. GSD phase execution
must not bypass the scope fence, validation deny-list, worktree isolation,
validation, review, or verification — in practice, GSD phase commands do not run in
a repository carrying `.ai/project.yaml`.

Decided during the M3 pilot and first recorded in `M3-PILOT-REPORT.md`. Promoted to
**ADR-002** on 2026-09-12; the register at
`dev-orchestration-spec/docs/decisions/README.md` is authoritative and this section
is a pointer to it.

The evidence, from the 2026-09-01 `helmfast-OS` pilot — twelve runs, three reaching
`COMPLETE_LOCAL`.

What the evidence showed:

- **No observed failure would have been prevented by a stronger intent layer.** The
  dominant failure mode was Codex adapter and structured-output unreliability — four
  of twelve runs, a third of the pilot. Planning was not implicated in any failure.
- **The run that exhausted its remediation budget was a worker no-op**
  (`20260901-053245`): an approved one-word README correction was never applied, three
  cycles running, and the independent implementation reviewer caught it each time from
  the diff. The plan was correct and approved. Better planning would have changed
  nothing; reading the diff rather than the builder's narration is what caught it.
- **The scope fence fired on real writes**, escalating a run that wrote outside it. It
  is the control the pilot most clearly validated, and GSD subagents do not honour it.
  Admitting an executor that ignores the fence would regress the one control that
  demonstrably works.
- **The lifecycle gap GSD would fill is already filled.** `helmfast-OS` carries a
  nineteen-document in-fence `planning/` tree — ADRs, specs, plans, adversarial
  reviews — and still has no `.planning/`. The overlap remained prospective throughout
  the pilot.

The original analysis, retained because it is what the decision rests on:

#### Background as assessed pre-pilot

GSD is installed globally (67 skills under `~/.claude/skills/gsd-*`) and is therefore live
in every repository. No helmfast repo has used it — there is no `.planning/` anywhere — so
this is a prospective overlap, not a migration.

The overlap: `.planning/` and `.ai/runs/` both answer "why did this change"; both systems
produce an artifact called the plan; both produce review and verification verdicts under
different schemas.

**The risk worth naming:** GSD subagents know nothing about the scope fence or the
validation deny-list. A `/gsd:execute-phase` run inside `helmfast-OS` would edit
business-knowledge trees freely. The fence protects dev-orchestration runs, not the repo.

The natural seam, if it is taken later, is `WORKFLOWS.md` §13 — GSD's `PLAN.md` imported as
an external approved plan with `plan_origin: external`, `.planning/` staying the intent
layer and `.ai/runs/` the evidence layer. Plan import is already built in M2, so this
option stays open at no additional cost.

Each `AGENTS.md` recorded the ownership question as open until the decision above was
taken; `dev-orchestration/AGENTS.md` now records the decision. The other onboarded
repositories still carry the "unsettled" wording and are stale until updated.

Note also that `helmfast-OS` already carries two prior trails — `.superpowers/sdd/` and a
hand-written `planning/` folder. Any eventual consolidation should account for four
systems, not two.

### 11.2 Does minimization actually help?

The design now asserts that smaller, scoped context produces better runs. That is
a hypothesis, not a measured result, and it can fail in the other direction: an
agent starved of a constraint fails just as surely as one that drowned in
irrelevant material.

Two mechanisms exist to answer it from evidence rather than impression — the
`exclusions` field recorded on every context packet, and the pilot's context
quality questions, which explicitly ask whether any failure was caused by
withheld context. Neither is exercised until M2 and M3.

### 11.3 Codex alpha instability

The installed build is `0.146.0-alpha.9.2`, bundled inside a desktop application that
auto-updates. Its `exec` contract may change without notice. Mitigated — not eliminated —
by capability detection at `doctor` time and by recording the resolved binary path and
version in every manifest.

---

## 12. Out of scope for this build

Deferred to V1.1, with pilot evidence informing each: the human approval gate
(`IMPLEMENTATION-PLAN.md` phase 8); `docs audit` / `docs migrate` / `docs check` tooling
(phase 14); the `substantial` and `high_risk` tiers beyond their policy guardrails; composite `feature` / `bugfix` / `refactor` commands (phase 13); telemetry
export (phase 18); self-hosting (phase 17); GitHub Actions.

Permanently out of scope for V1, per the source specification: autonomous push, merge,
deployment, package publication, production data mutation, and any web dashboard.

---

## 13. M1 outcome (2026-08-26)

M1 shipped `dev-orch init` and `dev-orch doctor`, and all three pilot repositories
were onboarded on a local `ai/onboard-dev-orchestration` branch in each. Nothing was
pushed, merged, or deployed.

**The `ruff` gap.** §9 lists `ruff check` as a `helmfast-OS` validation command. It is
not registered, and the table above is superseded on that point. `ruff` is configured
nowhere in `helmfast-OS` and is not installed in its `.venv` — `python -m ruff --version`
exits 1 with `No module named ruff`; only a stale `.ruff_cache/0.15.18` survives from an
earlier run. Per the rule that a command must be proven before it becomes policy, only
`test` was registered. `pytest --collect-only -q` collected **651 tests, exit 0**. The
absence and the reinstatement condition are recorded in `helmfast-OS/.ai/context.md`.

**`npm run build` passed.** `helmfast-site`'s `astro check && astro build` exited 0 —
0 errors, 0 warnings, 0 hints across 13 files, 6 pages built. `build` is registered on
that evidence. `npm run deploy` remains deny-listed and unregistered. One oddity worth
remembering: the first foreground invocation produced no output and was killed after
7 minutes; an immediate detached re-run finished in about 4 seconds.

**`helmfast-app` registers no validation command at all.** It contains no `package.json`,
`pyproject.toml` or `Makefile` — ten documentation files and a README. `validation: {}`
is a finding, not an omission.

**Checks that did not pass, and why that is correct.** `doctor` reports
`! repository clean` in `helmfast-OS`, naming 7 pre-existing uncommitted paths. That is
the dirty-worktree guard doing its job on real in-flight work; it was left untouched and
verified byte-identical afterwards. `! codex goal mode` reports in all three repositories:
the installed Codex build advertises the goals feature but exposes no headless entry
point, so execution will use `codex exec`, exactly as §11.3 anticipated.

**Defect found while onboarding, and fixed.** `GitRepo.run_git` returned
`stdout.strip()`, which removes the leading space from the *first* porcelain line only.
Both consumers slice `line[3:]` assuming an intact `XY<space>PATH` prefix, so the first
listed path lost a character: a dirty `.env.example` was reported as `env.example`. It
only surfaces when the first entry is an unstaged modification (` M path`) — the most
common dirty state — and the whole suite missed it because every fixture dirties the tree
with an *untracked* file (`?? path`, no leading space). The guard always blocked
correctly; it mis-named one file while doing so.

Fixed at the source: `run_git` gained a `strip` keyword, `status_porcelain` passes
`strip=False` and rstrips only the trailing newline. Both call sites needed no change once
given intact input. Regression tests cover an unstaged dotfile, multiple dirty paths with
the unstaged one first, and a mixed staged/unstaged/untracked case, asserting exact path
strings. Verified live against `helmfast-OS`, which now reports `.env.example` correctly.

This is the pilot earning its keep: twelve reviewed tasks and several rounds of mutation
testing did not surface it, because every test constructed the one dirty state that hides
it. Running the real binary against a real repository did, immediately.

**Context minimization, measured.** The §10a directive was applied for the first time.
In `helmfast-OS` the only pre-existing agent-facing file, a 4,930-byte `CLAUDE.md`, became
a 373-byte pointer (−92%); every rule it carried was already stated elsewhere in the
repository — the conflict-of-interest boundary in nine other files, two of them tests that
enforce it; the ICP band in fourteen. The original is archived verbatim at
`docs/archive/CLAUDE-original.md`.
The first measurement was worse than that headline: total persistent surface *rose*
from 4,930 to 7,199 bytes, because the onboarding inventory had been written into
`.ai/context.md` — a file carried on every invocation. In `helmfast-OS` that inventory was
2,724 of 4,399 bytes, so a one-time audit record occupied most of a permanent file. The
plan's Step 1b specified that location and the plan was wrong: DOCUMENTATION-STANDARD §15
already designates `.ai/docs-audit/` for audit output, and an audit mandated by the
context-minimization directive must not itself become permanent context.

Each inventory was moved to `.ai/docs-audit/agent-surface.md`. Final persistent surface:

| Repository | Before | After | Composition |
|---|---|---|---|
| `helmfast-OS` | 4,930 | **4,458** | AGENTS.md 2,427 + context.md 1,658 + CLAUDE.md pointer 373 |
| `helmfast-app` | 0 | 3,276 | AGENTS.md 2,221 + context.md 1,055 |
| `helmfast-site` | 0 | 3,706 | AGENTS.md 2,255 + context.md 1,451 |

`helmfast-OS` is the only before/after comparison, and it fell 10% while changing character
entirely: a doc index that restated the repository became a contract of invariants plus two
facts that genuinely cannot be inferred — that `pipeline/runtime/prompts.py` renders its
templates out of three fenced-off directories, so a template edit cannot be completed
autonomously; and that `ruff` is absent, with the condition for reinstating it. The other
two had no agent-facing files at all, so their surface is new rather than reduced.

Every generated `AGENTS.md` sits at roughly 2.2–2.4 KB against the 8,192-byte persistent
budget — around 28% of the ceiling, leaving room for repository-specific invariants that
onboarding has not yet discovered.

---

## 14. Final review and safety-control hardening (2026-08-27)

A whole-branch review found three Critical defects that the fifteen per-task
reviews structurally could not: each saw one diff, and all three live in the
seams between tasks or between a control and its call path. All are fixed.

**A role marked read-only ran with write access.** `verifier` was in
`READ_ONLY_ROLES` but bound to codex, and `CodexAdapter` never reads
`allowed_tools` — it emits `-s workspace-write`. Task 9 defined the field,
Task 11 consumed it, Task 10 shipped before Task 12 existed, and Task 12 then
marked roles read-only without knowing codex ignores it. Registry tests
asserted on the request, adapter tests on the argv, and nothing asserted the
join. `build_request` now fails closed when a read-only role is bound to an
adapter that does not declare `read_only_review`, which catches roles that do
not exist yet. The `verifier` default moves to claude, superseding §2 of
`CONFIGURATION.md`: role bindings are configurable, but a declared restriction
being actually enforced is not negotiable.

**The runtime git guard checked only the first argument.** git accepts global
options before the subcommand, so `run_git("-c", "k=v", "push", …)` presented
`-c` first, matched no prohibited name, and reached `subprocess`. The deny-list
was also incomplete by construction — it named `push` and `merge`, so `pull`
(a fetch *plus a merge*) passed under a name containing neither word, as did
`reset`, `clean`, and `stash`. `run_git` now requires an allow-list of the
seven verbs the framework actually invokes.

**`init --force` destroyed uncommitted work.** It rewrote `AGENTS.md`
unconditionally, and a hand-added invariant that was not yet committed is in no
commit and no stash. `ensure_clean` existed for exactly this and was wired only
into `doctor`, the one command that cannot cause the harm.

**The source scan never looked for `deploy`.** A planted
`subprocess.run(["npx","wrangler","deploy","--env","production"])` left the
suite green at 180 passed, and a sentinel proved a green run *executed* it.
`gh`, `npx`, `wrangler` and `curl` never pass through `run_git`, so for those
the scan is the only control there is. It now parses each module and inspects
literal `subprocess` argv lists — `deploy` and `publish` cannot be matched as
bare substrings, because this document's own `AGENTS.md` template states the
prohibition in prose.

Also closed: the scope fence failed open on the three most natural ways to
write it (a bare `exclude: [secrets]` excluded nothing, `*` crossed `/`, and
`../../etc/passwd` passed under the default include); `healthcheck` raised
instead of reporting unavailability; and plan immutability was a TOCTOU check
rather than an atomic create.

The lesson worth carrying into M2 is narrower than "test more." Every one of
these passed a test suite that exercised the code thoroughly. What none of them
had was a test asserting the *join* between two components, or a control traced
from its definition to a production call site. Three controls in this milestone
have no production caller at all — by design, since M1 builds the substrate and
not the loop — and two of them contained defects that would have become live
the moment M2 wired them.
