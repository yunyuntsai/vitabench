---
name: workflow-optimize
description: Generic closed-loop agent/benchmark optimization. Two-phase loop: Phase 1 runs eval, clusters failures, and writes a design report; Phase 2 implements one fix, retests it, and closes the loop log. Benchmark-specific facts come from an ADAPTER-<benchmark>.md in this same folder (tau-bench retail/airline via ADAPTER-TAUBENCH.md; tau2-bench airline/retail/telecom/banking_knowledge via ADAPTER-TAU2BENCH.md; vitabench delivery/instore/ota/cross via ADAPTER-VITABENCH.md). Use when optimizing an agent's prompt/policy/tool wiring from eval results for any benchmark, or when onboarding a new benchmark to this loop.
disable-model-invocation: true
---

# Workflow Optimize (Generic Loop)

A benchmark-agnostic, two-phase loop that turns eval results into a design
report (Phase 1) and a validated, targeted fix (Phase 2). This file defines
**the loop shape** — nothing here should mention a specific benchmark, CLI
flag, or file path; those live in this same folder's `ADAPTER-<benchmark>.md`
files.

## Step 0: Select The Benchmark

Before Step 1.0 can run, resolve two things:

- **Which adapter.** If the user names a benchmark explicitly, use its
  `ADAPTER-<benchmark>.md` in this folder. If ambiguous or unspecified, list
  the `ADAPTER-*.md` files present here and ask. If none exists yet for the
  target benchmark, that's the first thing to write — see *Adapting This
  Loop To A New Benchmark* below — not a reason to guess.
- **Which setup doc, if any.** If a `SETUP-<environment>.md` exists in this
  folder matching the current execution environment, read it too. It's
  deployment-specific (auth, proxies, credentials, venv) — **not** part of
  the adapter contract, and it's fine for it to not exist at all.
- **Which run config, if any.** A benchmark may define a run-config file
  format (scenario, models, sampling/seed, confidence policy, stop
  conditions) so these don't need restating in chat every invocation — see
  that benchmark's adapter doc for whether it defines one and its schema.
  If the user names a config file (or path/benchmark implies a default
  one), load it for every value it covers. **Precedence: explicit values
  stated in this invocation > the named config file > the adapter's own
  defaults.** State which config file (if any) you loaded, and any values
  you overrode from it, before starting Phase 1.

If the loop shape itself needs to change (a new phase, a new stop
condition), edit this file directly — that change should benefit every
benchmark, not just one. If a benchmark fact needs to change, edit that
benchmark's `ADAPTER-<benchmark>.md` only. Don't duplicate a fact across an
adapter and this file — if you find the same thing stated in both, that's
drift; keep it in the adapter alone.

## Adapter Contract

An adapter doc must answer these 7 subsections, organized into two blocks:
**Block 1: Agent Customization** (what's being optimized) and **Block 2: Eval
Customization** (how to run it and turn results into a trustworthy fix
decision). This is a fixed, benchmark-agnostic checklist — the questions
never change when onboarding a new benchmark, only the answers do. If any are
missing or ambiguous, stop and ask the user rather than inventing an answer —
1.2, 2.3, and 2.4 in particular gate safety (what's editable), correctness
(what a fix even means), and trust (whether a result is real), and are not
areas that reward a plausible-sounding guess.

| Block | # | Subsection | Answers | Used in |
|---|---|---|---|---|
| 1 | 1.1 | Agent architecture | What artifacts define the agent's behavior (prompt/policy, tool/action defs, routing); what the interaction loop looks like (single-turn? agent + simulated user? multi-agent?); where/how reward is computed | Step 1.0, Step 1.2 (interpreting evidence) |
| 1 | 1.2 | Editable vs. forbidden surface | What's safe to edit (the editable surface — policy / agent instruction / tool docstrings, co-equal and root-cause-driven) vs. never edit (ground truth, evaluator logic, benchmark data) | Step 2.1 (gates every edit) |
| 1 | 1.3 | Agent environment (scenario + sampling) | How to select a scenario/domain; how to draw a reproducible optimization sample and a disjoint held-out sample | Step 1.0, Step 2.4 |
| 2 | 2.1 | Eval invocation | The command/script to run a set of cases and get raw results back, including a fast single-case rerun and (ideally) re-analyzing a checkpoint without rerunning | Step 1.1, Step 2.2, Step 2.4 |
| 2 | 2.2 | Result/artifact schema | Where pass/fail lives, where per-case evidence (transcript/trace/diff) lives, exact field nesting | Step 1.1 (confirms the run produced something analyzable) |
| 2 | 2.3 | Failure taxonomy (+ actionability) | Category names + the mechanical rule for deriving each cluster key from a raw result; which failure causes are out of scope for the agent (evaluator gap, simulator drift, infra flake, missing external fixture, etc.) for this benchmark specifically | Step 1.2, Stop condition 2 |
| 2 | 2.4 | Confidence policy | Whether the benchmark is stochastic; if so, trial counts and pass thresholds; if not, single-trial is correct and this subsection should say so explicitly | Step 2.2, Step 2.4 |

Two subsections resolve mechanically from the others rather than needing
independent design effort: 1.3's scenario/sample selectors follow from
however 1.1 exposes domains/splits, and 2.2's artifact schema follows from
however 2.1's runner is built. Answer them anyway, explicitly — don't leave
them implicit in the runner's source.

### Adapting This Loop To A New Benchmark

1. Write a new `ADAPTER-<benchmark>.md` **in this same folder**, answering
   all 7 subsections above. Do not copy another benchmark's adapter doc and
   edit around the edges — derive each subsection from how the new
   benchmark actually works (its own agent surface, its own reward signal,
   its own failure modes). This is the benchmark provider's checklist: every
   subsection must resolve to a concrete answer (a file path, a command, a
   rule) before the loop can run against that benchmark — none of the 7 are
   optional or answerable in the abstract. If an adapter doc already exists
   for another benchmark here (e.g. `ADAPTER-TAUBENCH.md`), read it as a
   **reference for structure and expected rigor per subsection only** — how
   concrete a cluster-key rule needs to be, how much evidence an
   actionability claim needs, what "answering" a subsection actually looks
   like in practice. Its actual *content* (file paths, taxonomy keys,
   commands) is specific to that benchmark and never transfers.
2. Do not edit this file's Phase 1 / Phase 2 / Stop Conditions to special-case
   the new benchmark. If the loop shape itself genuinely needs to change,
   that's a deliberate generic-loop change, considered separately from
   onboarding one more benchmark.
3. If the new environment needs its own auth/proxy setup, add a separate
   `SETUP-<environment>.md` to this folder — don't fold it into the adapter
   doc.

## Artifact Conventions (never benchmark-specific)

Three files carry the loop's history. Names and lifecycle are fixed
regardless of benchmark:

| File | Scope | Lifetime |
|---|---|---|
| `OPTIMIZATION-LOG.md` | Across all loops — trajectory + per-loop hypothesis & outcome | Durable, append-only — one file for the whole optimization history |
| `EVAL-DESIGN-REPORT-loop<N>.md` | One loop — clusters + this loop's fix priority | Written once in Phase 1, Step 1.3 — never overwritten by a later loop |
| `REVISION-LOG-loop<N>.md` | One loop — fix evidence: root cause, diff, before/after | Written once in Phase 2, Step 2.3 — never overwritten by a later loop |

**Loop number `N`** = one past the last `## Loop N` header in
`OPTIMIZATION-LOG.md`, or `1` if that file doesn't exist yet. Reuse the same
`N` for this loop's report, revision log, and `## Loop N` section — all three
artifacts for one loop share one index.

### `OPTIMIZATION-LOG.md` Schema

The only one of the three files that's both read and written every loop.
Create it from this template on Loop 1 if it doesn't exist:

```markdown
# OPTIMIZATION-LOG

Append-only, one section per loop, newest last. Read before Phase 1; close
after Phase 2's retest. Do not repeat a fix priority a prior loop already
disproved. Each loop's design report and revision log are separate files —
`EVAL-DESIGN-REPORT-loop<N>.md` / `REVISION-LOG-loop<N>.md` — not sections of
this file.

**Source of truth for all pass-rate metrics** (baseline, revised, Δ, cluster
inventory). The per-loop `REVISION-LOG-loop<N>.md` files carry evidence, not
metrics — don't re-record numbers there.

## Trajectory
| Loop | Date | Baseline | Fix priority | Result | Δ | Status |
|---|---|---|---|---|---|---|
| 1 | <date> | <n>/<m> (<pct>%) | `<cluster-key>`: <one-line direction> | <n>/<m> (<pct>%) | <±delta> | CLOSED |

## Loop <N> — <date>
- **Baseline:** <batch/checkpoint path> — <n>/<m> (<pct>%)
- **Clusters (key ×count [tag]):** `<cluster-key>` ×<count> [new|recurring|regressed] · `<cluster-key>` ×<count> [tag] · ...
- **Fix priority chosen:** `<cluster-key>` — <one-line what/why, including why this one if it won a tie>
- **Fix implemented:** <one-line description of the edit> — <files/surfaces touched>
- **Retest:** <command or trial summary> → <result> → CREDITED | NOT CREDITED (see `REVISION-LOG-loop<N>.md`)
- **Regression check:** <held-out batch result, or "deferred" with why>
- **New/deferred clusters surfaced:** `<cluster-key>` — <case id(s)> — <one-line note>
- **Status:** CLOSED | OPEN
```

**Record clusters by the exact key adapter subsection 2.3 derives.** Copy it
verbatim from that cluster's `###` header in `EVAL-DESIGN-REPORT-loop<N>.md`
— never re-derive or re-paraphrase it here — so the report and the ledger
agree character-for-character and the next loop's Step 1.2 can match by key,
not by paraphrase. The `[tag]` in **Clusters** is the *incoming* status this
loop (new/recurring/regressed, decided in Step 1.2 against this same file); a
cluster's outcome shows up as that same key either disappearing from a later
loop's Clusters line (resolved) or persisting (untouched/regressed) — there
is no separate "outcomes" field to keep in sync.

Keep entries tight — reference batch/checkpoint paths, don't inline
transcripts (those belong in that loop's own `REVISION-LOG-loop<N>.md`).

## Phase 1: Eval + Design Report

### Step 1.0: Load Inputs

- Read the adapter doc's subsections 1.1, 1.2, and 1.3 (architecture,
  editable surface, task sampling) — these define what you're optimizing and
  what you're allowed to touch.
- Read `OPTIMIZATION-LOG.md` if present. Do not re-propose a fix a prior loop
  already disproved for a cluster you can match by key.
- Determine loop number `N`.

### Step 1.1: Run Eval And Normalize Artifacts

- Run the eval using adapter subsection 2.1 (eval invocation), on the
  optimization task sample defined by adapter subsection 1.3.
- Confirm the run produced the artifact shape defined by adapter subsection
  2.2 — a pass/fail summary plus per-case failure evidence. If it didn't,
  stop; a batch you can't analyze is worse than not running one.
- Reuse a prior batch instead of rerunning when nothing has changed since
  (adapter subsection 2.1 should say how to re-analyze a checkpoint without
  rerunning).

### Step 1.2: Cluster Failures And Choose One Fix Priority

- Apply adapter subsection 2.3 (failure taxonomy) to derive a cluster key per
  failure. This gives the **WHAT** — what went wrong (which call was missed,
  extra, or wrong-arg).
- **Derive the WHY, not just the WHAT.** The mechanical key does not tell you
  *why* the agent made the wrong call, and the why determines the surface and
  whether it's fixable. For the chosen priority (and any cluster you defer),
  read the task's **stated intent/purpose** — wherever the benchmark records
  it (adapter 2.2 names the field; for tau2, `tasks[*].description.purpose`
  and `user_scenario.instructions.task_instructions`). The designer often
  states the scenario explicitly (e.g. "Testing that the agent refuses a
  disallowed cancellation under user pressure"). That stated intent is a
  **ground-truth WHY source** — cleaner than inferring motive from dialogue
  and cleaner than an LLM judge opining on transcripts. Use it to classify the
  *motive* behind the wrong action (e.g. *caves to user pressure on an
  ineligible action* vs *acts speculatively without a request* vs *misread the
  user's intent* vs *genuinely cannot reason the right action*). Different
  motives map to different surfaces and different fixability: a wrong action
  with a rule/eligibility motive is fixable; the same wrong action with a
  reasoning-capability motive is not. Reading the intent is a **one-time,
  subject-invariant investment** — the tasks don't change when the subject
  model does, so the intents learned in one loop carry to every subject.
- Treat the largest cluster as the default priority, but validate it against
  1-2 per-case evidence records (transcript **and** task intent) before
  committing — a cluster key is a pointer to a bucket of failures, not proof
  they share one root cause. A "catch-all" or residual key (one whose
  derivation is "anything not caught by a more specific rule", e.g. tau2's
  `DB`) is **not** exempt from this: its size is exactly why it's a priority
  candidate. "Catch-all" describes how the key was *derived*, not whether the
  cluster is *actionable* — that is decided only by the evidence read below,
  never by the key name. A large catch-all is a signal to **sub-cluster** it
  (per adapter 2.3's procedure for residual keys), not to skip or defer it on
  the label.
- Tag each cluster against `OPTIMIZATION-LOG.md`: `new` (not seen before),
  `recurring` (seen, unresolved), or `regressed` (previously fixed, now
  failing again).
- Apply adapter subsection 2.3's actionability rules to rule out clusters
  that aren't actually the agent's fault before picking a priority from them.
  **The same 1-2 representative-transcript read required before *proposing*
  a fix is required before *deferring* a cluster as non-actionable** — you
  cannot defer on the key name or a "looks capability-bound" intuition alone.
  For every cluster you defer, cite the specific evidence checked (which
  transcript, which task intent/purpose, which divergence) and the specific
  2.3.1 reason it maps to, **including the WHY**: a non-actionable verdict
  requires the motive (read from the task intent) to be genuinely
  non-agent-fixable, not merely that one attempted surface didn't credit. A
  defer with no cited WHY is invalid: re-read the task intent before logging
  it, or it cannot count toward stop condition 2.
- If two clusters tie, prefer the one with the clearer, more directly
  actionable shared root cause (motive) over raw count.

### Step 1.3: Write Design Report And Open Log Entry

Write `EVAL-DESIGN-REPORT-loop<N>.md` (a new file — never an overwrite of a
prior loop's report) with this schema:

1. **Header** — one line of run metadata (whatever adapter subsections 1.1
   and 1.3 need to reproduce the run: model(s), scenario, task sample,
   checkpoint path) plus a machine-readable `**Baseline pass rate:** <n>/<m>
   (<pct>%)` line — Phase 2 parses this as the before-number.
2. **Failure clusters** — the section header is always literally `## Failure
   clusters`, present every loop even with only one failure left. Never
   rename it (e.g. to "Root-cause investigation") and never fold it into the
   header summary.

   Each cluster gets its own `###` subsection with this literal,
   machine-parseable header — every field mandatory, in this order:

   ```
   ### <cluster-key> (×<count>) [<tag>]
   ```

   - **`<cluster-key>`** — copied verbatim from adapter subsection 2.3's
     mechanical derivation (e.g. tau-bench's
     `ACTION::mutIdx0:expected->actual`) — never invented or paraphrased.
     This exact string is what you copy into `OPTIMIZATION-LOG.md`'s
     Clusters line below — same characters in both places.
   - **`<count>`** — number of cases in this cluster.
   - **`[<tag>]`** — exactly one of `[new]`, `[recurring]`, `[regressed]`,
     the literal word from Step 1.2's tag decision. Never replaced by prose
     (`deferred`, `excluded`, `fix priority`) — those observations go in the
     body text under the header; the header's tag is always one of the three
     literal words.

   Body below the header: 1-2 representative case IDs with evidence, the
   root cause in prose, and — for `recurring`/`regressed` — what a prior
   loop already tried and why this loop's fix priority isn't a repeat.
3. **Root-cause analysis** — evidence from representative per-case records
   for the chosen fix priority specifically, **starting with the task's stated
   intent/purpose** (the WHY, per Step 1.2) and then the transcript evidence
   (the WHAT). State the motive in one line (e.g. "caves to user pressure on
   an ineligible cancellation"), then the action-level evidence that confirms
   it. The motive, not the mechanical key, is what picks the surface.
4. **Implementation recommendation** — exact files/surfaces to edit, scoped
   by adapter subsection 1.2. **Surface selection is motive-driven, not
   ranking-driven and not WHAT-driven.** Adapter 1.2's editable surfaces
   (policy / agent instruction / tool docstring) are co-equal candidates,
   **not a priority order** — edit the one the *motive* (from the task intent)
   actually lives in, as shown by the per-case evidence, not a default. Do
   **not** default to policy markdown when the evidence points elsewhere: a
   wrong-argument call from an ambiguous schema is a **tool-docstring** fix;
   a missing domain-agnostic behavioral
   rule is an **agent-instruction** fix; a missing domain procedure is a
   **policy** fix. State which surface and why, with the evidence that locates
   the root cause there.
5. **Fix priority** — exactly ONE `<cluster-key>` selected for Phase 2. Must
   be consistent with `OPTIMIZATION-LOG.md` (Step 1.0): don't re-pick a
   priority a prior loop already disproved without saying why.
6. **Deferred clusters** — every other cluster, logged with a one-line
   reason **that cites the specific 2.3.1 evidence** (which representative
   transcript was read, which divergence ruled it non-actionable), so the
   next loop doesn't lose them and the defer is auditable. A bare label like
   `DB ×9 (catch-all, non-actionable)` is not a valid reason — per Step 1.2,
   a defer requires the same transcript read as a propose, or it's invalid.

Then open or append `## Loop N` in `OPTIMIZATION-LOG.md` per its schema
above, with baseline, the **Clusters** line copying each `###` header's
`<cluster-key> ×<count> [<tag>]` verbatim, and the chosen fix priority. Leave
Retest/Regression/Status fields open until Phase 2's retest closes the loop.

## Phase 2: Agent Revision

### Preconditions

- Confirm the repo is git-managed; note any dirty working-tree state before
  editing.
- Do not overwrite user changes.
- Commit only when explicitly requested by the user or the host workflow.
- Scope the loop to the one selected fix priority unless the user explicitly
  approves a broader change.

### Step 2.1: Implement The Selected Fix

- Edit only the surface adapter subsection 1.2 marks as editable. Choose
  **which** editable surface by where the root cause lives (root-cause-driven,
  per the design report's implementation recommendation) — adapter 1.2's
  surfaces are co-equal for *correctness*; policy markdown is **not** the
  default. A fix belongs to the surface the evidence points to (policy / agent
  instruction / tool docstring), not whichever is listed first.
- **Prompt-fragility awareness.** The editable surfaces are all part of the
  agent's system prompt, so any edit perturbs the model's behavior on *every*
  task that prompt reaches — not just the target cluster. On a weak or
  high-temperature subject model this is severe: a small, correct-for-target
  addition can regress unrelated stable passers (a flight-change clause
  breaking a name-change task), making the fix net-negative even when the
  target flips. Two disciplines bound this: (a) **keep the edit minimal and
  scoped to the specific root cause** — do not rewrite surrounding text,
  reorder paragraphs, or bundle a second clarification into the same loop; (b)
  when the root cause genuinely maps to more than one surface (a clarification
  that could live in either a tool docstring or a policy rule), prefer the
  **most localized** surface that still reaches the root cause (adapter 1.2
  ranks the surfaces by perturbation footprint). Localization never overrides
  a clear root-cause mapping — if the evidence points to one surface, use it —
  but it breaks ties and sizes the edit. Step 2.4's unrelated-passer guards
  catch what minimization doesn't prevent.
- Never touch the surface adapter subsection 1.2 marks as forbidden — even if
  doing so would "fix" more failures. Editing ground truth or evaluator logic
  redefines what correct means; it does not fix the agent.

### Step 2.2: Optional Single-Case Reproducer

If the benchmark supports a fast targeted rerun of one case (adapter
subsection 2.1), use it for quick before/after evidence on the
representative failure before a full retest. This establishes causation for
that one case only — not cluster-wide generalization, which Step 2.4 is for.

### Step 2.3: Write Revision Log

Write `REVISION-LOG-loop<N>.md` (a new file — never an overwrite of a prior
loop's revision log) with this schema. Evidence and change history only — no
pass-rate metrics beyond the restated baseline; those live solely in
`OPTIMIZATION-LOG.md`.

1. **Header** — this loop's baseline pass rate (context only, restated from
   the design report).
2. **One fix card per cluster actually fixed this loop**, using this
   literal, machine-parseable header plus mandatory fields:

   ```markdown
   ### <cluster-key>
   - **Failing cases:** <case id(s)> · design-report cluster ref
   - **Root cause:** <what specifically went wrong and why>
   - **Change:** <file:line or surface, from adapter subsection 1.2's
     editable list> — <diff or exact text of the change>
   - **Before (fail):** <case id> — evidence excerpt showing the wrong
     behavior (source per adapter subsection 2.2's artifact schema)
   - **After (pass):** <case id> — evidence excerpt showing the corrected
     behavior, from the retest in Step 2.4 (or Step 2.2's single-case
     reproducer, if run)
   ```

   `<cluster-key>` must be the exact string from that cluster's `###` header
   in this loop's `EVAL-DESIGN-REPORT-loop<N>.md` — never re-derived.
3. **Retest result** — a trial-by-trial table if adapter subsection 2.4
   required multiple trials, otherwise a single pass/fail line, plus the
   credit decision (`CREDITED` / `NOT CREDITED`) against adapter subsection
   2.4's threshold.

**How this ties to `OPTIMIZATION-LOG.md`:** the ledger stays high-level (fix
priority → result → next hypothesis) and its `Fix implemented`/`Retest`
lines point to this file for the exact diff and evidence — don't duplicate
diffs or transcript excerpts into the ledger.

### Step 2.4: Final Retest And Regression Check

- Retest the target cases using adapter subsection 2.1, applying adapter
  subsection 2.4's credit policy (trial count + threshold, or "single trial
  is sufficient" if the benchmark is deterministic).
- **Include unrelated-passer guards in the retest.** Alongside the target
  cases, retest a few passers that are outside the target cluster and
  unrelated to the edited surface — tasks the fix is not meant to touch. Their
  purpose is to detect prompt-fragility regressions (Step 2.1): an edit that
  perturbs the shared prompt can break unrelated tasks. Prefer consistent
  (3-of-3) passers as guards — a regression on one is strong evidence the edit
  caused it — but don't filter out borderline passers: report **both `pass^1`
  and `pass^3`** for every target and guard (adapter 2.4) so the variance is
  visible, and weight a borderline guard's drop (e.g. 3/3→2/3) as weaker
  evidence than a stable guard's drop (3/3→0/3), since some of it may be
  run-to-run noise rather than the edit. Honest metrics, not a stability
  filter, are how non-determinism is handled here.
- **An unrelated-passer regression disqualifies the fix.** If a stable,
  unrelated guard regresses after the edit, the fix is a net-negative prompt
  perturbation: revert it, even if every target case flipped to pass. Do not
  credit a target win bought with an unrelated loss — that is not an
  attributable gain (Stop condition 4). Record it in the revision log as
  `NOT CREDITED — prompt-fragility regression on <guard case ids>`, then
  either re-scope the edit to a more localized surface (Step 2.1) or defer the
  cluster as capability-bound if no localized surface reaches the root cause.
- If budget allows, run the held-out sample from adapter subsection 1.3 to
  check for regressions. When interpreting new failures there, use adapter
  subsection 2.3's actionability rules to separate edit-caused failures from
  pre-existing or flaky ones.

### Step 2.5: Close The Loop

Update `OPTIMIZATION-LOG.md` with:

- Retest batch/result and whether the fix is credited.
- Regression check result, if run.
- Remaining clusters and deferred hypotheses.
- Final status for this loop.

Commit only if explicitly requested, after the log is closed.

## Stop Conditions

Stop when any of these hold, evaluated at the end of Phase 2:

1. **Target pass rate is met.**
2. **All remaining clusters are non-actionable** per adapter subsection 2.3's
   actionability rules — cite the specific reason per cluster, not just the
   conclusion.
3. **Loop budget is reached.**
4. **Diminishing returns** — a loop yields no attributable gain, or creates an
   unresolved regression. An **unrelated-passer regression from prompt
   fragility** (Step 2.4) is the clearest case: it disqualifies the fix for
   that loop, and if no more-localized surface reaches the root cause, the
   cluster is capability-bound for this subject model — defer it, don't keep
   reformulating the same prompt edit. **Beware the false capability wall:**
   a no-credit result is *not* by itself evidence that a cluster is
   capability-bound. A no-credit from the **wrong surface** (a misdiagnosed
   motive — e.g. framing "caves to pressure on an ineligible cancellation" as
   "over-acts speculatively" and fixing the behavioral surface instead of the
   policy eligibility rule) looks identical to a no-credit from a genuine
   capability gap. Before declaring a cluster capability-bound under this
   condition, re-confirm the **WHY** from the task intent (Step 1.2) and that
   the surface you tried was the one the motive actually lives in. Only when
   the right surface has been tried and the motive is genuinely reasoning-
   capability is the cluster non-actionable for this subject. A misdiagnosed
   root that credited on a different surface is a fixable cluster, not a
   capability wall.

At the stop point, report final pass rate, loop-by-loop trajectory, credited
fixes, and remaining actionable vs. non-actionable clusters with reasons.

## Architecture Escalation (when content-editing hits a fragility ceiling)

The loop's default scope is **the agent's knowledge** (policy content, agent-instruction text, tool
docstrings) **within the benchmark's fixed architecture** — i.e. the prompt the benchmark ships is
treated as a fixed delivery shape, and you only edit what it says. That keeps results comparable to
the released benchmark agent.

When that scope hits a **compounding-fragility ceiling** — every further content edit regresses
unrelated stable passers or erases prior loops' credited gains, because the whole policy enters every
task's prompt so any edit perturbs everything (observed on weak / high-temperature shared-prompt
subjects, where even a one-line docstring change crashed stable passers) — there is a structural next
step: **decouple the prompt per task/skill** so a rule for one topic loads only for that topic's
tasks. This is an **architecture change** (edit the policy *loader*, not just the policy *text*), so
it is **gated, not default**, and the gate has three hard parts:

1. **Explicit user opt-in, with the comparability caveat.** A per-task-loading agent is a *custom
   variant*, not the released benchmark agent — its scores are **not** comparable to baseline
   benchmark numbers and do not go on any leaderboard. State this to the user and get explicit
   approval before touching the loader. The default scope (knowledge edits, fixed architecture)
   never needs this opt-in.
2. **Anti-leakage — absolute.** Any task→section routing must use **user-visible signals only**
   (`user_scenario` / the live conversation / the user's stated reason for calling), **never**
   `evaluation_criteria.actions`, expected actions, ground truth, or any field the agent would not
   see at inference time. Routing on the expected answer pipes the answer into the prompt — any
   pass-rate gain from that is **cheating, not optimization**, and silently invalidates the whole
   run. If you cannot route without peeking at ground truth, do not escalate; stop at the ceiling.
   A safe fallback (load all sections when routing is uncertain) must always be present so a
   mis-route degrades to the shared-prompt agent, not to a broken one.
3. **Re-baseline after the architecture change.** A routed agent usually starts *lower* than the
   shared-prompt baseline (mis-routing + sections missing cross-cutting context). Re-baseline on the
   tuning split, then resume the per-motive loop — now **decoupled**: a section-A edit no longer
   enters section-B tasks' prompts, so fixes accumulate without compounding fragility.

**Concrete plan lives in the adapter** (it's benchmark-specific: where the loader is, how to split
the policy, what the user-visible routing signal is). The adapter must mark this as a gated
escalation outside its default editable surface (1.2) and name the loader file(s) explicitly. Report
the final result as **two clearly labeled numbers**: (a) shared-prompt + content-optimized
(benchmark-comparable, the ceiling) and (b) routed + section-optimized (custom architecture,
non-comparable, whether decoupling lifted the ceiling) — never present (b) as a benchmark score.
