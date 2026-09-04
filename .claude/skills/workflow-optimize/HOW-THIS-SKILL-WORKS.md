# How The `workflow-optimize` Skill Works

## Two-layer design

The skill is split into exactly two kinds of files:

- **`SKILL.md`** (generic loop) — defines the *loop shape*: two phases
  (Phase 1: eval → cluster failures → design report; Phase 2: implement one
  fix → retest → close the loop log), the stop conditions, and the schemas
  for the three artifact files. It never mentions a specific benchmark, CLI
  flag, or file path.
- **`ADAPTER-<benchmark>.md`** (e.g. `ADAPTER-TAUBENCH.md`) — answers a
  fixed checklist of benchmark facts (paths, commands, taxonomy) the loop
  needs in order to actually run. It never mentions loop mechanics.

Optionally:

- **`SETUP-<environment>.md`** (e.g. `SETUP-CRESTA-PROXY.md`) holds
  deployment-only concerns (venv, API keys, proxy quirks) that aren't true
  facts about the benchmark itself. A different environment with real API
  keys can reuse the same adapter unchanged and just skip this file.
- **`configs/<name>.yaml`** lets a benchmark define scenario/models/
  sampling/stop-conditions once, so they don't need restating in chat every
  invocation.

## How the two layers interact

The linkage is **Step 0's dynamic loading + indirection**, not a hardcoded
reference between files:

1. **Step 0** (top of `SKILL.md`) resolves which `ADAPTER-*.md` to read (by
   name, or by asking if ambiguous), which `SETUP-*.md` matches the current
   environment, and which config file to load — all before the loop itself
   starts.
2. Every step in Phase 1 / Phase 2 of `SKILL.md` refers to **"adapter
   subsection X.Y"** instead of a literal path or command — e.g. Step 1.1
   says "run the eval using adapter subsection 2.1," Step 2.1 says "edit
   only the surface adapter subsection 1.2 marks as editable." This
   indirection is what lets the exact same `SKILL.md` run against tau-bench
   today and a different benchmark tomorrow without editing the loop file.
3. If the same fact ever ends up stated in both files, that's treated as
   drift — it should live in the adapter alone, never duplicated.

## The 7-subsection adapter contract

Every `ADAPTER-<benchmark>.md` must answer these 7 subsections. The
questions never change when onboarding a new benchmark — only the answers
do.

| Block | # | Subsection | What it answers |
|---|---|---|---|
| 1. Agent Customization | 1.1 | Agent architecture | What defines behavior (prompt/policy, tools, routing), the interaction loop shape, where reward is computed |
| | 1.2 | Editable vs. forbidden surface | What's safe to edit (agent policy) vs. never (ground truth, evaluator logic, benchmark data) |
| | 1.3 | Scenario + sampling | How to pick a domain and draw a reproducible optimization sample plus a disjoint held-out sample |
| 2. Eval Customization | 2.1 | Eval invocation | Command to run cases, a fast single-case rerun, and (ideally) re-analyzing a checkpoint without rerunning |
| | 2.2 | Result/artifact schema | Where pass/fail and per-case evidence (transcript/trace/diff) live, exact field nesting |
| | 2.3 | Failure taxonomy + actionability | Mechanical rule for deriving a cluster key per failure; which failure causes are out of the agent's control for this benchmark |
| | 2.4 | Confidence policy | Whether the benchmark is stochastic; if so, trial counts and pass thresholds for trusting a result |

Subsections **1.2, 2.3, and 2.4** gate safety (what's editable), correctness
(what a fix even means), and trust (whether a result is real) — the skill
says to stop and ask rather than invent a plausible-sounding answer for
these three specifically.

## Artifact conventions (fixed, never benchmark-specific)

| File | Scope | Lifetime |
|---|---|---|
| `OPTIMIZATION-LOG.md` | Across all loops — trajectory + per-loop hypothesis & outcome | Durable, append-only |
| `EVAL-DESIGN-REPORT-loop<N>.md` | One loop — clusters + this loop's fix priority | Written once in Phase 1 |
| `REVISION-LOG-loop<N>.md` | One loop — fix evidence: root cause, diff, before/after | Written once in Phase 2 |

`OPTIMIZATION-LOG.md` is the single source of truth for pass-rate metrics;
the other two carry evidence, not numbers. Cluster keys are copied
verbatim between all three files — never re-derived or re-paraphrased —
so loops can match clusters by exact key across runs.

## What we want a benchmark provider to supply

Onboarding a new benchmark means writing one new file,
`ADAPTER-<benchmark>.md`, containing:

- Concrete file paths for the editable vs. forbidden surface — not just
  categories.
- A real, runnable eval command (plus a fast single-case rerun and
  ideally a "re-analyze a checkpoint without rerunning" mode).
- A **mechanical** (not LLM-guessed) rule for turning a raw result into a
  cluster key. Tau-bench's is a plain diff over the trajectory and reward
  metadata — no judge model required by default.
- Explicit actionability rules: which failure causes this benchmark's
  provider considers "not the agent's fault" (e.g. tau-bench: transient
  API errors, user-simulator drift, borderline/ambiguous grading).
- An explicit stochasticity / trial-count policy, even if the honest
  answer is "deterministic, 1 trial is sufficient."

Everything else — the three artifacts' schemas, the loop phases, and the
stop conditions — is already fixed in `SKILL.md` and applies unchanged to
any benchmark that adds an adapter.

## Validated in practice

This isn't just a paper design — Loop 1 has actually run end-to-end against
the clean `tau_bench/` retail agent: `OPTIMIZATION-LOG.md`,
`REVISION-LOG-loop1.md`, and the `results/eval-batch-*/` runs all exist from
that real run, confirming the adapter → generic-loop wiring works as
designed.
