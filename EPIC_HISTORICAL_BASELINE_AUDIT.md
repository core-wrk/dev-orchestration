# EPIC historical baseline and local-model feasibility audit

**As of:** 2026-11-05. **Scope:** read-only examination of this checkout and discovered local historical records; no inference, benchmark, service configuration, model download, or framework changes. This report addresses software and historical evidence; hardware capacity belongs to the separate hardware audit. **Evidence labels:** Verified = directly observed in source or saved artifacts; Inference = interpretation with stated limits; Recommendation = proposed future work; Unknown = not established. Aggregate counts are descriptive, not quality ground truth.

## 1. Executive summary

- **Verified:** 338 distinct saved run IDs were found in seven project repositories, plus four copies in a release worktree. They span 2026-09-01 through 2026-11-05 by run ID. There are 41 `COMPLETE_LOCAL`, 187 `FAILED`, 94 `ESCALATED`, eight `CANCELLED`, six `BLOCKED`, and two `PAUSED_USAGE`. These are saved statuses, not independent judgments about product or model quality.
- **Verified:** 230 runs have at least one usage-bearing event; 1,103 of 1,141 recorded invocation events contain a usage object. Their reported total is 1.227 billion tokens; this is partial, sums repeated cached input over turns, and is not a context-window measurement. By role, the worker is 53.0% and implementation review 26.8% of measured usage.
- **Verified:** Claude envelopes report $499.82 across recorded calls, but billing statements and subscription arrangements were not established. This is a provider-reported cost figure, not proven actual expenditure or marginal savings. Codex actual costs are unknown. No historical price schedule was applied.
- **Verified:** The current implementation runs Codex and Claude command-line agents, validates structured responses, and implements isolated worktrees, checkpoints, verification and approval gates. Its normal Codex adapter passes `--ignore-user-config` and does not pass `--oss` or an endpoint setting; replacing a model alias does not by itself route to Ollama or an OpenAI-compatible server.
- **Inference:** The best early targets are shadow implementation review, bounded planning/reconciliation subtasks, and isolated trivial implementation. Keep frontier decisions and existing gates during measurement. Evidence is insufficient to claim any local model can already replace a role safely or economically.

## 2. Verified architecture and implementation status

The checkout at Git commit `7754806` is a Python command-line orchestration system, with role templates, adapter protocol, Pydantic contracts, context assembly, a workflow state machine, artifact store, and Git isolation. Source, not documentation, determines the following. Historical runs may have used earlier versions or different per-project configuration.

| Role | Current default | Reasoning | Notes |
|---|---|---|---|
| Classifier | Codex `luna` → `gpt-6-luna` | high | Read-only; initial short timeout and retry on timeout. |
| Planner | Claude `claude-opus-5-5` | medium | Generated plans; external approved plans may bypass generation. |
| Plan reviewer | Codex `sol` → `gpt-6.1-sol` | high | Read-only structured review. |
| Plan reconciler | Claude `claude-sonnet-5-5` | high | Invoked when review requires reconciliation; not every run. |
| Implementer | Codex `gpt-6-luna` | high | Worktree write access. |
| Implementation reviewer | Claude `claude-opus-5-5` | high | Read-only structured review. |
| Remediation | Same implementation worker binding | high | No independent default remediation model; includes validation repair and findings remediation. |
| Final verifier | Codex `gpt-6-luna` | high | Read-only structured criterion verdicts. |
| Dependency analyst | Inherits verifier when enabled unless overridden | inherited | Optional context discovery. |

`src/dev_orchestration/adapters/registry.py` defines defaults and read-only capability checks; `config/models.py` exposes `adapter`, `model`, `reasoning`, and `timeout_seconds` per role. Precedence is framework defaults, global `~/.dev-orchestration/config.yaml`, then project `.ai/project.yaml`. The inspected global file sets a Codex binary; this project's role overrides are empty. Run `roles.json` captures configured bindings, not necessarily actual execution for each role. The only adapters registered by `default_registry` are `codex` and `claude`; a custom protocol implementation still needs registration in code. A model accepted by an existing CLI can be changed through configuration, but transport, credentials, CLI capabilities, and safety enforcement cannot be assumed interchangeable.

**Sequence:** `workflow/tiers.py` implements trivial = classification → execution → deterministic validation → final verification; standard adds planning → plan review → conditional reconciliation before execution, and implementation review afterward. Substantial adds the human-approval stage, with actual gate controlled by policy/profile; this project's substantial approval is false and high-risk true. High-risk is not an implemented ordinary tier path in `stages_for`; do not infer end-to-end high-risk execution from the approval setting. Imported external plans, reuse, restarts and optional dependency analysis alter the path.

**Contracts and safety:** `adapters/base.py` defines `AgentRequest`/`AgentResult`; Codex uses `exec --json`, sandbox `read-only` or `workspace-write`, and `--output-schema`; Claude uses headless JSON, `--json-schema`, and restricted tools for read-only roles. Tool execution is delegated to the provider CLI, not a generic orchestration-level tool loop. Claude's restricted path uses `--tools`, `--allowedTools`, and skip-permission mode; this requires adapter-specific review before reuse. `workflow/invoke.py` validates structured output using Pydantic and retries once on schema failure; nonzero provider exit raises an error. Classifier has a separate timeout retry. Reconciliation and findings remediation are bounded to two cycles; validation repair is limited to one. Usage-limit and interruption handling supports saved pause/resume under conditions; it is not general automatic model failover.

**Context and verification:** `context/assembler.py` builds provenance-labelled, budgeted context from declared sources and role-specific artifacts; the final prompt has a 96,000-byte cap, with explicit failure for essential non-truncatable content. Byte budgets are not model-token or full agent-context limits: tool output and conversation turns grow separately. `workflow/stages.py` provides the plan, diff, validation evidence, findings and criteria to appropriate roles; deterministic validation and scope checks complement model review. `workflow/runner.py`, `recovery.py`, `git/worktree.py` and `artifacts/store.py` implement worktree setup, checkpoints, approval, cancellation, recovery and terminal artifacts; safe continuation is conditional on recorded integrity checks. No live safety behavior was tested here.

## 3. Historical run inventory

**Verified, high confidence for discovered directories, not for all machine history.** A run is one non-hidden immediate directory under a discovered `.ai/runs` containing a readable manifest. Four release-worktree run IDs match IDs in this checkout and their manifest SHA-256 hashes match; counted once. This establishes duplicate manifests, not byte-for-byte equality of every artifact. All 342 raw directories had readable manifests. Hidden `.goals` records were not counted as runs. Dates below use IDs, not verified start/finish timestamps.

| Repository | Unique runs | Completed | Failed | Escalated | Canceled | Other saved status |
|---|---:|---:|---:|---:|---:|---|
| claimcode-app | 127 | 2 | 59 | 56 | 2 | 6 blocked, 2 usage-paused |
| claimcode-payer-rules-engine | 52 | 7 | 31 | 13 | 1 | — |
| dev-orchestration | 123 | 19 | 87 | 15 | 2 | — |
| core-wrk-OS | 18 | 9 | 6 | 3 | 0 | — |
| helmfast-OS | 14 | 4 | 2 | 7 | 1 | — |
| avie-data-engine | 3 | 0 | 1 | 0 | 2 | — |
| helmfast-site | 1 | 0 | 1 | 0 | 0 | — |
| **Total** | **338** | **41** | **187** | **94** | **8** | **8** |

Tiers: substantial 229, standard 98, trivial 11. No saved `PAUSED_INTERRUPTED` status was found; this does not establish that no interruptions occurred, as resumed or failed runs can conceal prior interruptions. The status is a snapshot and repeat attempts are distinct run IDs, not distinct tasks. Of 338, 319 have criteria files, 289 a versioned plan, 246 a plan review, 219 validation artifacts, 181 a patch, 142 an implementation review, and 116 final verification artifacts. A mechanical minimum evidence bundle (criteria file + validation file + verification + any patch) exists in **88 runs**; presence is not proof of reproducibility, independence, completeness, or quality label. 29 runs contain token events with at least one missing usage object, including two with no reported usage; 203 have usage on every *recorded* token event, not necessarily every historical invocation.

**Coverage limits:** searched project trees for `.ai/runs` and inspected the configured global worktree root, local Codex/Claude history locations and observability project. Removed, archived, external-volume, differently named and earlier-system records can be missed. CLI histories were not reliably joined to run IDs; do not add their potentially overlapping counts. No full transcript or billing reconciliation was performed. The records are strongly concentrated in self-hosting and two ClaimCode projects, include retries and incomplete runs, and overrepresent substantial work; failure proportions must not be generalized to all EPIC tasks or used as model success rates.

## 4. Token consumption and cost analysis

**Method, verified:** sum `token_usage` events from unique-run `events.jsonl` files. `adapters/tokens.py` normalizes Codex input to uncached plus cached, and Claude separates ordinary input, cache read and cache creation. Total = uncached input + cached read + cache creation + output. Reasoning is a subset of output and is not added. Each event may aggregate many agent turns; repeated cache reads are counted as the provider reported them. Counts are not unique prompt tokens. Confidence is high for arithmetic over saved events, low for actual full historical consumption because absent events, early versions and failed calls are not recoverable. `attempt` is a per-role invocation counter, not a reliable retry label.

| Role | Reported tokens | Share of measured | Recorded calls, with usage | Provider/model observed |
|---|---:|---:|---:|---|
| Implementation worker, including repairs | 650,434,952 | 53.02% | 257; 247 reported | Codex `gpt-6-luna`, historical `gpt-5.6-luna` |
| Implementation reviewer | 329,304,048 | 26.84% | 146; 146 | Claude `claude-opus-5-5` |
| Plan reviewer | 118,793,467 | 9.68% | 257; 254 | Codex `gpt-6.1-sol`, historical `gpt-5.6-sol` |
| Plan reconciler | 65,169,206 | 5.31% | 77; 77 | Claude `claude-sonnet-5-5` |
| Planner | 51,779,367 | 4.22% | 36; 36 | Claude `claude-opus-5-5` |
| Classifier | 5,853,099 | 0.48% | 258; 236 | Codex Luna variants |
| Verifier | 5,455,280 | 0.44% | 110; 107 | Codex Luna variants |
| **Total** | **1,226,789,419** | **100%** | **1,141; 1,103** | Missing usage is not zero consumption. |

For the 230 runs with any measured usage: mean 5,333,867; median 4,010,409; nearest-rank P90 12,283,516; maximum 22,086,592. These are sums of available events per run, including partial records, not complete-run estimates. Sample selection and aggregation make them unsuitable as expected usage of a new run. By tier: substantial 1,009,502,640 across 163 measured runs; standard 190,434,001 across 60; trivial 26,852,778 across seven. By project: claimcode-app 493,287,140/64; payer-rules 241,386,083/50; dev-orchestration 443,844,802/98; core-wrk 48,271,394/18. No usage-bearing event was found in the other three projects. These subsets are neither random nor complete.

Claude's saved `total_cost_usd` sums to **$499.82**: planner $81.42, reconciler $78.45, implementation reviewer $339.95. **Unknown:** whether these numbers represent billed amounts, list-price equivalents or subscription accounting; rates, effective pricing dates and invoices are absent. They must not be presented as verified marginal API charges. Highest *reported Claude envelope* sums per run are $10.37 (payer-rules, 2026-11-01 15:01), $10.23 (payer-rules, 2026-10-11 03:29), and $9.76 (payer-rules, 2026-10-29 01:18). These omit unpriced Codex usage and may omit calls. No defensible total-cost ranking of patterns or estimate is available. No external prices were applied.

**Unknown:** tool call counts, reliable per-stage retry/remediation token attribution, actual billed cost, complete execution-duration distribution, peak request context and model-window requirements. Saved duration is per recorded completed invocation, and no comprehensive wall-clock duration analysis was validated. `attempt > 1` includes normal subsequent calls and review cycles. Historical `validation_repaired` (62) and `remediated` (42) events establish repair activity, not its token cost. Provider reports of zero reasoning tokens can mean not exposed, not that reasoning was absent.

## 5. Role-specific quality analysis

**Verified, descriptive only:** the unique-run artifacts contain 399 plan-review versions: 116 PASS, 277 CHANGES_REQUIRED and six BLOCKED; their findings are 316 blocking, 451 important and 143 minor. The 170 implementation-review versions contain 110 PASS and 60 CHANGES_REQUIRED, with 74 blocking, 100 important and 140 minor findings. Versions are not independent tasks, and the same finding can recur. There are 159 `plan_reconciled` events, 62 `validation_repaired` and 42 `remediated` events, not necessarily unique runs or successful repairs. The 116 final verifier artifacts show 53 PASS, 43 CHANGES_REQUIRED, 19 BLOCKED and one ESCALATE. These values are model/workflow decisions, not independently confirmed truth.

| Role | What the artifacts establish | Unknown without independent adjudication |
|---|---|---|
| Planner | Some generated plans, imported plans, revisions, reviews, and reconciliations exist. External imports limit observations of the planner itself. | Completeness, missing requirements, downstream ambiguities, true scope violations, and proportion needing significant revision. |
| Plan reviewer | Saved finding severity and review outcomes can be counted. | True/false positives, unique valid findings, missed defects and acceptance/rejection rates. Reconciliation is not itself proof that a finding was correct. |
| Implementer | Worktree patches, validation and repair events document some attempts. | First-pass success, incorrect code rates, actual tool errors and scope deviation rates across all runs; failure status alone cannot establish model fault. |
| Implementation reviewer | Findings and multiple review versions exist. | Defects independently confirmed, false positives, rejected findings and missed defects. |
| Final verifier | Structured outcomes and criteria verdicts exist for a subset. | Alignment with real acceptance and false approval/rejection without independently checking criteria, revision and validation evidence. |

No finding was classified as correct or incorrect for this audit: a reviewer assertion, later model agreement, or a repair alone is insufficient independent evidence. No verified contradiction between verifier verdict and validation is asserted without case-level adjudication. Selection and failed-run censoring prevent performance comparisons across roles or models.

## 6. Failure-mode analysis

**Verified process signals:** classification timeouts, schema escalation paths, failed validation, reconciliation, remediation, blocked and escalated runs exist as code paths or saved event types. The inventory recorded 18 classifier-attempt-failed events and five remediation-exhausted events; these are events, not independently established root causes. Failure classes can include environment, framework, human decision or product issues. **Unknown:** a defensible frequency ranking of actual model errors, repeated defects or prompt-caused failures.

**Inference / future hypotheses:** constrain JSON and provide concise evidence references to reduce malformed or unsupported findings; supply explicit scope and acceptance criteria to target omissions; use failed-command feedback for repair. Evaluate each before adopting. LoRA or experimental weight editing might affect task adherence, but no evidence here establishes training benefit or identifies a causal parameter-level defect. Weight editing is exploratory and should not be treated as a known fix.

## 7. Local-model substitution opportunities

Ranking uses measured shares only where available and is conditional on evaluation, not a claim that these models fit this MacBook Air.

| Rank / workload | Suggested mode | Evidence, demands and consequence |
|---|---|---|
| 1. Implementation-review finding extraction | Shadow; initially partial offload | 26.84% measured tokens and $339.95 reported envelope cost. Large diff/context, repository reads, structured JSON and subtle reasoning; missed blockers have high consequence. Frontier reviewer remains authoritative. |
| 2. Reconciliation / plan-review subtask | Shadow, then local-first with frontier escalation | Together 14.99% measured tokens. Ground findings against a bounded plan and evidence, but decisions affect downstream scope. Structured output; selective repository reads may be necessary. |
| 3. Trivial implementation | Shadow in an isolated replay; possible local-first only after gates | Worker is 53.02% measured tokens but only 11 total trivial historical runs, seven with usage. Requires a real tool-capable agent, write isolation, tests and scope checks; account for repair and time cost. |
| 4. Classifier | Shadow and compare | Low measured token share (0.48%), clear structured labels, but underclassification affects safeguards; default to higher tier on uncertainty. |
| 5. Final verifier | Shadow only initially | Low measured share (0.44%); criterion-by-criterion evidence supports evaluation but false pass is consequential. Keep frontier plus deterministic checks. |
| 6. Full planner and nontrivial implementation | Frontier initially | Broad repository exploration, ambiguity and downstream consequences; historical ground truth is insufficient. A 4.22% measured planner share undercounts externally imported plans. |

Savings are not proportional to token share: cache pricing, subscriptions, machine energy, latency, retry rates and hardware utilization matter. Rank quality and safety before cost. Context capacity must be determined from per-turn traces or replay instrumentation, not run totals.

## 8. Dataset and benchmark readiness

**Verified:** manifests and context references, plan versions, reviews/findings, patches, validation logs, remediation records, verifier verdicts, saved role/model bindings, Git base-commit fields and checkpoints can form candidate input/output pairs. Coverage counts are in §3. Plan review and verification artifacts do not themselves independently label correctness; originals may rely on external plans, missing source states, provider tools, mutable dependencies, or unavailable worktrees. Determine whether referenced commits and patches actually resolve for each case before eligibility. `roles.json` is configuration, not proof a role was invoked. Never assume a `PASS` is a gold label.

**Evaluation:** candidate cases need accessible frozen base commits, exact criteria, complete input artifacts, reproducible tests and independently adjudicated outcomes. Independent regression tests, recorded command outputs, and human review can support labels, but historic outputs are hypotheses. **Training:** even a complete pair is only a candidate; document provenance, rights, confidentiality, review decision and outcome, filter sensitive content, and independently verify its target. Frontier-generated output, proprietary code and third-party dependencies require applicable provider terms, repository ownership and license review before reuse for training or weight adaptation. This audit does not adjudicate those terms and creates no dataset.

**Recommendation:** group by repository, goal, linked retries, imported plan hash and near-duplicate patch; split whole families and preferably time periods before preparing any examples. Keep a sealed held-out set inaccessible to prompt tuning, sample selection, training and weight editing. Deduplicate across train/dev/test. Preserve a separate final holdout for one predeclared comparison; no feedback from it into training.

## 9. Integration compatibility assessment

| Route | Verified state | Implication |
|---|---|---|
| Codex CLI local mode | Both installed `codex exec --help` and the configured binary advertise `--oss` and `--local-provider` for Ollama/LM Studio. | EPIC's adapter passes neither, and explicitly ignores user config. CLI support alone does not prove a given local model supports coding tools, schema or this framework. |
| Existing adapters | Codex and Claude registered; `AgentAdapter` is a protocol and registry controls bindings. | Arbitrary aliases can be supplied, but providers cannot be added only by changing a role config unless an adapter is already registered. |
| OpenAI-compatible HTTP / Ollama / llama.cpp | No direct endpoint field or HTTP adapter found in the current registry/config path. Ollama and `llama-server` executables are present; running services, installed models and compatibility were not tested. | No verified current EPIC endpoint integration. llama.cpp might be reachable through a compatible intermediary, but this is unverified; do not equate an HTTP API with an agent runtime. |

**Recommendation:** first evaluate a narrowly scoped extension to the Codex adapter/config to select its advertised local provider while retaining sandbox, expected schema, role binding, timeouts, event accounting and offline configuration. Verify installed CLI behavior and exact supported option combinations before design. If the CLI route cannot preserve those contracts, implement and register a dedicated local agent adapter with an explicit tool-execution loop and enforcement; a bare chat endpoint is insufficient. This is a future change, not implemented or tested. Provider-specific prompts, context overflow and truncation, reasoning-effort incompatibility, JSON adherence, timeouts, cancellation/process groups, permissions, logging and retry semantics require contract tests. An explicit policy is needed for when to escalate to frontier; do not silently fail over or weaken read-only controls.

## 10. Observability readiness

| Need | Existing evidence | Read-only remote monitoring prospects / gaps |
|---|---|---|
| Run ID, tier, state and next action | Manifest, events, `dev-orch status` | Can read indexed artifacts over a separately secured channel; no built-in remote monitor established. |
| Active role/model | State, provider-start record and configured `roles.json`; token event follows invocation | Derivable with caveats; stage and saved PID do not prove process liveness, and model binding is not necessarily actual runtime selection. |
| Progress and errors | Checkpoints, state transitions, terminal reasons, provider failure artifacts | Summaries possible; events are not a fine-grained live progress stream and may contain sensitive text. |
| Token use and cost | Completed `token_usage` events; `dev-orch usage` | Post-call partial totals; missing calls and provider-reported cost caveats apply. No verified live token streaming or billing feed. |
| Validation and approvals | Validation artifacts, states, approval receipts | Read-only status can expose summaries, subject to access controls and stale-state handling. |
| Pause and cancel | Usage/interruption pause and resume paths; `dev-orch cancel` records and targets provider process | Monitoring alone cannot safely perform control. Remote cancellation needs authenticated, authorized control and race/liveness checks; no general user-requested pause or remote control was established. |

The separate `helmfast/agent-observability/dashboard` has implemented local native Codex/Claude history ingestion, a localhost server and a private SQLite review store. Its README says it excludes Codex exec jobs from the main conversation list when identified. The inspected dashboard source search found no `.ai/runs` or EPIC event integration. Its existence is not evidence of EPIC remote integration. Treat a read-only EPIC artifact adapter as future work; do not expose its localhost server or sensitive histories to a network without a separate security design.

## 11. Recommended initial experiments

**Recommendation, no benchmark executed:**

1. **Shadow implementation review:** replay saved diffs, criteria, plan and validation into an untuned local model and a prompt-specialized local model. Compare finding-level precision, recall against independently confirmed seeded and naturally occurring defects, severity, evidence citations and missed blockers. Frontier output is a comparator, not truth.
2. **Bounded plan reconciliation:** replay plan/review pairs with adjudicated findings. Score whether each supported blocker is addressed, unsupported findings are handled explicitly, scope remains intact and JSON validates. Keep a frontier reviewer in control.
3. **Trivial implementation replay:** use the seven usage-bearing trivial runs only as an exploratory pool, not enough for statistical claims. Freeze base commits, isolate worktrees, run identical deterministic checks and independently review patches. Measure first-pass and eventual success, scope, tool failures, wall time, energy and repairs.

**Four conditions:** A = current frontier configuration at a recorded version and tool policy; B = untuned local open-weight model with neutral role instructions; C = same local weights with role-specific prompts/config; D = adapted or experimentally modified local weights after rights review. Hold task inputs, base revision, tests, allowed tools, time budget and output schema constant where technically possible; record unavoidable differences and run order. Use independent isolated copies, synthetic data and no external side effects. Record cold/warm starts, model and quantization, context and generation settings, memory, throughput, tool and model invocations, per-turn input/output/cache/reasoning when available, elapsed time, energy, actual incremental cost, repairs and escalations. Never infer local cost from subscription pricing.

Start with a feasibility pilot of 10–20 eligible cases per role if enough independent families exist; then target at least 30–50 per stratum for an exploratory comparative estimate, with confidence intervals and explicit power calculations before any replacement decision. The present 88 mechanical bundles are a screening pool, not 88 qualified cases. Stratify by repository, tier, context size, task type, outcome and repair history; avoid treating repeated retries as independent. Assign family groups to development and holdout before tuning; retain an untouched final holdout. Missing eligibility, outcome or telemetry is reported, not silently excluded. Blind independent adjudicators to condition, predefine success as all mandatory criteria plus deterministic checks, no scope violation and no unresolved independently confirmed blocker; capture partial outcomes and uncertainty. Report paired differences and confidence intervals, failure severity, timeouts, invalid JSON and escalation rates; do not claim equivalence from a small nonsignificant test.

## 12. Open questions and missing evidence

- Which historical sessions can be linked to EPIC by stable run/session IDs without ambiguous overlap? Are earlier or nonstandard artifact locations missing?
- Can each candidate's base commit, dependencies, exact context, worktree state and validation environment be reconstructed? Are artifacts intact and rights cleared?
- Which reported Claude amounts were actually charged, and under what plan? What were historical effective rates and Codex marginal costs? Billing records are necessary for savings claims.
- What is the peak per-turn context distribution including tools, and how do local models handle long contexts, JSON schemas, permission enforcement and tool calls in the installed CLI version?
- Which recorded findings and verifier decisions survive independent reproduction? How many runs failed for environment or framework causes rather than model behavior?
- Is a compatible local endpoint actually available, and what are measured memory, speed, energy and concurrency on the target Mac? This report makes no hardware feasibility claim.

## 13. Evidence appendix and methodology

**Source references:** `src/dev_orchestration/adapters/{base,registry,codex,claude,tokens,usage}.py`; `config/{models,resolver}.py`; `context/{assembler,packet}.py`; `workflow/{tiers,stages,invoke,runner,recovery,reporting,validation}.py`; `domain/{run,enums}.py`; `artifacts/{store,events}.py`; `git/worktree.py`; `cli.py`; role templates under `src/dev_orchestration/roles/`; this checkout's `.ai/project.yaml`; and global `~/.dev-orchestration/config.yaml`. Current code and configuration are not historical version guarantees.

**Inventory sources:** discovered `.ai/runs` beneath `/Users/andrewodonnell/GitRepos/` in the seven repositories listed in §3 plus `dev-orchestration-release-wt`; immediate non-hidden run directories, manifests, event JSONL, and artifact filenames were inspected. Duplicate IDs were checked against original manifest hashes. Run data was aggregated in memory using Python's standard library; no analysis dataset or intermediate file was written. Usage totals come solely from saved event objects, using the provider-normalized total and not adding reasoning again. Artifact counts are file-presence checks; review outcomes and severities were parsed from versioned JSON; verifier outcomes from final-verification JSON. Status and tier come from saved manifests. Dates are run-ID prefixes; nearest-rank P90 is the sorted observation at `ceil(0.90 × 230)`.

**Other evidence:** configured Codex executable and PATH Codex `exec --help`; `~/ .dev-orchestration` global configuration and worktree location (without the space after `~`); local executable lookup for Ollama and llama-server; `/Users/andrewodonnell/GitRepos/helmfast/agent-observability/dashboard/README.md` and a source search for EPIC integration. CLI help only was invoked; no model requests were made. Histories, credentials and prompts are not reproduced here. The discovery and summaries are snapshots; active or newly created runs and unsurveyed locations may change totals.
