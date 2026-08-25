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
| GSD relationship | **Deferred** — decide after M3 | See §11.1 |

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
Enforcement happens in three independent places, because a rule agents are merely *told*
about is a rule they drift on:

1. **Context assembly** — excluded paths are never read, and never reach an agent prompt.
2. **Post-execution diff check** — the run's diff is compared against the fence. Any file
   touched outside `include` fails the run, regardless of what the agent reported.
3. **`doctor`** — prints the active fence, so it is never a silent policy.

The fence governs what agents may **modify**. It does not govern what repository code may
**reference**: `pipeline/runtime/prompts.py` legitimately reads three excluded trees as
template roots, and that continues to work.

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

**Acceptance:**
- [ ] One trivial-tier run completes on `helmfast-OS`.
- [ ] One standard-tier run completes on `helmfast-OS`.
- [ ] No remote push occurs.
- [ ] Run artifacts are legible to a future investigator without chat history.
- [ ] At least one framework improvement is identified from pilot evidence.
- [ ] The GSD boundary decision (§11.1) is made on evidence.

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

## 11. Open decisions

### 11.1 GSD boundary — deferred to post-M3

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

Until decided, each `AGENTS.md` records the ownership question as open rather than
prescribing an answer.

Note also that `helmfast-OS` already carries two prior trails — `.superpowers/sdd/` and a
hand-written `planning/` folder. Any eventual consolidation should account for four
systems, not two.

### 11.2 Codex alpha instability

The installed build is `0.146.0-alpha.9.2`, bundled inside a desktop application that
auto-updates. Its `exec` contract may change without notice. Mitigated — not eliminated —
by capability detection at `doctor` time and by recording the resolved binary path and
version in every manifest.

---

## 12. Out of scope for this build

Deferred to V1.1, with pilot evidence informing each: the human approval gate
(`IMPLEMENTATION-PLAN.md` phase 7); `docs audit` / `docs migrate` / `docs check` tooling
(phase 13); the `substantial` and `high_risk` tiers beyond their policy guardrails; composite `feature` / `bugfix` / `refactor` commands (phase 12); telemetry
export (phase 17); self-hosting (phase 16); GitHub Actions.

Permanently out of scope for V1, per the source specification: autonomous push, merge,
deployment, package publication, production data mutation, and any web dashboard.
