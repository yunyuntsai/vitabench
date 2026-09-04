# Adapter: tau-bench

Fills the adapter contract required by `SKILL.md`,
organized into two blocks: **agent customization** (what's being optimized)
and **eval customization** (how to run it and turn results into a trustworthy
fix decision). Read the generic loop file first; this doc only answers "what
is true about tau-bench specifically."

Environment/auth setup (venv, API keys, proxies) is **not** part of this
adapter — see `SETUP-CRESTA-PROXY.md`.

---

## Block 1: Agent Customization

### 1.1 Agent Architecture

The "agent" is a generic LLM tool-calling loop (`ToolCallingAgent` by
default), not a hand-written state machine. A scenario (`retail` or
`airline`) supplies:

- A system policy prompt: `tau_bench/envs/<scenario>/wiki.md`.
- Tool schemas + implementations: `tau_bench/envs/<scenario>/tools/*.py`,
  each exposing `get_info()` (schema shown to the LLM) and `invoke()`
  (actual mock-DB mutation logic).
- Mock domain data (users, orders/reservations, products/flights).
- Test tasks: a hidden user instruction plus expected final actions and/or
  required output strings.
- A simulated user, itself an LLM (`LLMUserSimulationEnv`), which only
  reveals information incrementally per the task instruction.

Interaction loop per task: env sends the task instruction to the simulated
user, which produces the first message; the tool-calling agent alternates
between responding to the user and calling tools; the env executes tool
calls against the mock DB; the episode ends on a terminate tool (e.g.
`transfer_to_human_agents`) or the user emitting `###STOP###`. Reward is
computed once, at episode end, from the final mock-DB state and any required
output strings.

**This architecture claim is a hypothesis, not settled fact, until checked
against real evidence** — confirm it by reading at least one real passing
transcript and one real failing transcript (`failure-details/<task_id>.json`
`traj`), not just this source code, before relying on it in Step 1.2.

### 1.2 Editable Vs. Forbidden Surface

**Editable** (in order of typical leverage):

1. `tau_bench/envs/<scenario>/wiki.md` — policy text. Most fixes land here.
2. `tau_bench/envs/<scenario>/tools/*.py` `get_info()` — tool name,
   description, parameter schema shown to the LLM. Safe to clarify an
   ambiguous parameter that's causing wrong-argument calls.

**Forbidden** — editing these doesn't fix the agent, it redefines what
"correct" means for every task that touches it:

- `tools/*.py` `invoke()` — the actual DB-mutation logic. This is ground
  truth / environment behavior, not agent policy.
- Task files (`tasks.py`, `tasks_test.py`, `tasks_train.py`, `tasks_dev.py`)
  — expected actions/outputs.
- Reward calculation (`tau_bench/envs/base.py:calculate_reward()`).
- Mock data fixtures, unless the user explicitly asks to change the
  benchmark itself (out of scope for this loop by default).

### 1.3 Agent Environment (Scenario + Task Sampling)

- **Scenario selector:** `--env retail` or `--env airline`. Selects the
  tau-bench env, and therefore which `wiki.md`/`tools/*.py` are in scope.
- **Optimization sample:** the tasks used to discover failure clusters and
  choose fixes. Prefer an explicit `--task-ids` list when one is already
  chosen. Otherwise draw `N_opt` tasks with `--shuffle 1`, `--seed <seed>`,
  `--start-index 0`, `--end-index <N_opt>`.
- **Held-out sample:** a disjoint task sample used only for regression /
  final validation, never for choosing the fix. Prefer a separate
  `--task-split` when available; otherwise use the same `--shuffle`/`--seed`
  ordering with a non-overlapping range, e.g. `--start-index <N_opt>`,
  `--end-index <N_opt + N_test>`.
- **Task split:** `--task-split train|dev|test`. Prefer `train`/`dev` for
  the optimization sample and reserve `test` (or a disjoint range) for the
  held-out sample. Note: the `airline` env currently only wires up the
  `test` split in `env.py` — confirm a split exists for a scenario before
  relying on it.

---

## Block 2: Eval Customization

### 2.1 Eval Invocation

Adapter script: `eval_optimize.py` (wraps `tau_bench.run.run(...)`, then
mechanically clusters failures on top).

Fresh run on an explicit task list:

```bash
python3 eval_optimize.py --env retail \
  --model <model> --model-provider <provider> \
  --user-model <model> --user-model-provider <provider> \
  --task-split test --task-ids 3 7 12 \
  --num-trials 1 --max-concurrency 8 --log-dir results
```

Fresh run on a sampled range (see 1.3 for why):

```bash
python3 eval_optimize.py --env retail \
  --model <model> --model-provider <provider> \
  --user-model <model> --user-model-provider <provider> \
  --task-split train --shuffle 1 --seed <seed> \
  --start-index 0 --end-index <N_opt> \
  --num-trials 1 --max-concurrency 8 --log-dir results
```

Fast single-case rerun (Phase 2, Step 2.2):

```bash
python3 eval_optimize.py --env retail \
  --model <model> --model-provider <provider> \
  --user-model <model> --user-model-provider <provider> \
  --task-ids <target_task_id> --num-trials 1 --max-concurrency 1
```

Re-analyze an existing checkpoint without rerunning (e.g. reusing a prior
loop's retest as this loop's baseline):

```bash
python3 eval_optimize.py --env retail --skip-run \
  --checkpoint-path results/<prior-ckpt>.json --batch-dir results/eval-batch-<ts>
```

Optional LLM fault-assignment judge, for when cluster ownership is
ambiguous or an actionability call (2.3) needs evidence:

```bash
python3 eval_optimize.py --env retail --skip-run --checkpoint-path <ckpt> \
  --with-fault-judge --fault-judge-platform anthropic --fault-judge-model <model>
```

This runs `auto_error_identification.py` and merges `author`
(`user`/`agent`/`environment`) plus `fault_type`
(`called_wrong_tool`, `used_wrong_tool_argument`, `goal_partially_completed`,
`other`) into each failure.

### 2.2 Result / Artifact Schema

`eval_optimize.py` writes to `results/eval-batch-<ts>/`:

- `batch-summary.json` — `passed`/`total`/`passRate` plus passed/failed task
  ID lists.
- `failure-analysis.json` — `{passed, total, passRate, clusters, failures}`;
  `clusters` is `[{key, taskIds, count}]` sorted largest-first; `failures`
  is `[{taskId, clusterKey, detailPath}]`.
- `failure-details/<task_id>.json` — instruction, ground-truth
  actions/outputs, full `traj` (message-by-message transcript), and the
  specific divergence evidence for that failure.

Reward metadata nests one level deeper than it looks:
`result["info"]["reward_info"]` is a `RewardResult`
(`tau_bench/types.py`); `r_actions`/`r_outputs`/`outputs`/`gt_data_hash`
live under `reward_info["info"]`, not on `reward_info` directly.

### 2.3 Failure Taxonomy

No LLM is used for this — it's a plain diff over result metadata and
trajectory (`cluster_key_for_failure()` in `eval_optimize.py`).

**`r_actions` is a final-DB-state hash comparison, not a sequence match** —
`calculate_reward()` replays `task.actions` on a fresh mock DB and compares
the resulting hash to the live episode's actual DB state. Read-only calls
(`get_*`/`find_*`/`list_*`/`calculate`) never touch `self.data`, so they
cannot be the cause of a hash mismatch — but a weak model tends to sprinkle
extra lookups before the one DB-mutating call that matters, and a raw
positional diff would flag those as the "divergence." `first_action_divergence()`
filters both sequences to mutating-only calls before diffing for this
reason.

**This taxonomy is a hypothesis, not settled fact, until checked against
real evidence.** The mutating-only filter above exists precisely because the
first version of this taxonomy didn't have it: a plain positional diff over
*all* actions mis-diagnosed two real failures as "an extra
`find_user_id_by_*` lookup" when the actual bugs were a wrong `item_ids` and
a wrong `payment_method_id` on the real mutating call. That was only caught
by reading the actual failing transcripts, not by re-reading
`calculate_reward()` more carefully. Always read `failure-details/<id>.json`'s
full `traj` before proposing a fix from a cluster key alone.

| Key prefix | Meaning | How it's derived | Actionable by default? |
|---|---|---|---|
| `ACTION::mutIdx{i}:{expected}->{actual}` | Action-graded task; the agent's *mutating* tool calls diverge from `task.actions`' mutating calls at index `i` (read-only calls filtered from both sides first) | Positional diff of mutating-only tool calls extracted from `traj` vs. ground truth; either side is `MISSING` if its sequence ran out first | Yes — usually a real agent bug, but verify it isn't user-simulator drift (2.3.1) before committing to a fix |
| `OUTPUT::missing:{text}` | Output-graded task; a required output string was never said to the user | Directly from `reward_info.info.outputs` — the most reliable/granular signal available; use it whenever a task has `outputs != []` | Yes — rarely non-actionable |
| `ERROR::{message}` | The agent crashed mid-solve (exception in `Agent.solve`) | `info` has an `error` key instead of `reward_info` | No, by default — usually a transient API/infra failure (2.3.1); actionable only if the same error reproduces on a clean rerun |
| `UNKNOWN::...` | Reward failed but the mechanical diff found nothing — mutating-action sequence matched positionally, an output-graded task had no `False` entries, or there's no `reward_info` at all | Needs a manual transcript read; don't guess a fix from the cluster key alone | No, until a transcript read surfaces an actual agent-side gap |

#### 2.3.1 Determining Actionability

A cluster is non-actionable — safe to defer or drop from the loop's target
— only when it resolves to one of these, confirmed by evidence, not merely
because it *looks* like one of these:

- **Transient API/infra failure** — an `ERROR::` cluster caused by a
  provider outage or timeout, not agent logic. Confirm by rerunning clean.
- **User-simulator drift** — the LLM user simulator failed to execute its
  own scripted instruction (e.g. skipped a conditional branch, referenced
  the wrong order/reservation ID that isn't the one ground truth targets).
  Confirmed via full transcript read, not inferred from the cluster key.
  `--with-fault-judge`'s `author=user` tag is a strong candidate signal for
  this, but still verify against the transcript before logging it.
- **`UNKNOWN` cluster with no resolvable agent-side gap** — after a full
  transcript read, the mechanical diff found nothing and no policy/tool gap
  explains the failure either.
- **Borderline/ambiguous grading** — the agent's answer is defensible but
  diverges from a narrowly-specified ground truth (e.g. a total that
  includes/excludes an edge-case component the grader didn't anticipate).
  Flag as a possible over-narrow grading case rather than adopting a fix
  that would only chase the specific ground-truth number.

Always cite the specific evidence checked (which transcript, which
divergence) in `OPTIMIZATION-LOG.md` when tagging a cluster non-actionable —
not just the conclusion. A "non-actionable" tag with no cited evidence must
be re-verified before it can be used to satisfy the generic loop's stop
condition 2.

### 2.4 Confidence Policy

Given 2.2's artifact schema, this is the rule for how many runs it takes
before a pass/fail signal is trustworthy enough to act on — not a fact about
tau-bench's structure, but a policy for reading its (noisy) output.

Tau-bench is stochastic: with `--user-strategy llm` (the default), the
simulated user is a sampled LLM call on *every* task, every run — not an
occasional edge case, but a structural property of every single task. The
same task, same code, can pass one run and fail the next.

- **Fast optimization loop (default):** `--num-trials 1`. A single-trial
  pass is sufficient evidence during the loop; don't pay for 3x trials on
  every iteration.
- **Confidence mode (opt-in):** `--num-trials 3` when a result looks flaky,
  the failure is high-stakes, or stronger causation evidence is needed
  (e.g. before crediting a fix in the revision log).
- **Credit threshold:** single-trial pass for the fast loop; at least 2/3
  pass when confidence mode is used, preferring 3/3 for critical fixes.
- **Diminishing returns:** stop if repeated fast-loop runs show no
  attributable gain, or confidence-mode results show `Delta <= 0` or an
  unresolved regression.

Used in Step 1.2 (how much to trust one failing transcript before
clustering it) and Step 2.4 (how much to trust a retest before crediting a
fix) of `SKILL.md`.

## Run Config (Optional)

`SKILL.md`'s Step 0 lets a run config file supply 1.3/2.1/2.4's inputs plus
the generic loop's stop conditions, instead of restating them in chat every
invocation. Configs live in `configs/<name>.yaml` in this same skill folder
— one file per (scenario, sampling, model) combination you run repeatedly,
e.g. `configs/tau-bench-retail.yaml`.

**Three LLM roles, not one "model."** Conflating the agent under test with
the user simulator is how a run stops being comparable to anything measured
before — see `subject_model` / `simulator_model` below. Only `subject_model`
is under optimization; `simulator_model` should be treated as fixed
(hold_fixed: true) unless the user explicitly asks to change the benchmark
itself.

The config file holds only five sections — **roles, sampling, confidence,
concurrency, stopping**. It is not the place for accumulated results: any
fact discovered by running a loop (what a mechanism's task-id registry is,
which subject models have already consumed a holdout, what's true only for
one specific subject, a protocol for swapping subjects) belongs in
`OPTIMIZATION-LOG.md` instead — see `SKILL.md`'s Artifact Conventions, which
already designates that file as the durable, cross-loop record. Keeping
findings there and only sampling *policy* here is what lets the same yaml
serve any subject model without silently carrying over another model's
results.

Schema (every field optional — anything omitted falls back to this adapter's
own stated default, e.g. `--num-trials 1` for the fast loop):

```yaml
scenario: retail                     # --env retail|airline

# --- Roles -------------------------------------------------------
subject_model:                       # the agent being optimized: --model / --model-provider
  id: us.anthropic.claude-haiku-4-5-20251001-v1:0
  provider: bedrock
  api_key_env: <ENV_VAR>              # only needed for non-bedrock/non-anthropic providers
  baselined: true|false               # false = no measurements exist yet for this subject
  selection_evidence: <one-line — why this model, what alternates it beat>

simulator_model:                     # the measuring instrument: --user-model / --user-model-provider
  id: us.anthropic.claude-haiku-4-5-20251001-v1:0
  provider: bedrock
  hold_fixed: true                    # swapping this changes the benchmark, not the subject
  note: >                             # why it's fixed, e.g. it's the only source of
                                       # non-determinism and its paraphrases decide winnability

optimizer: claude-code               # reads results, writes fixes — not CLI-configurable;
                                      # it's the session running this skill

fault_judge:                         # optional 4th role (adapter 2.1's --with-fault-judge), off by default
  enabled: false
  platform: bedrock                  # --fault-judge-platform
  model: <model>                     # --fault-judge-model
  note: <when/why to enable>

# --- Sampling ------------------------------------------------------
tuning_sample:                       # 1.3 optimization sample — never used for regression checks
  task_split: train                  # --task-split
  task_ids: "0-29"                   # explicit --task-ids, or use seed/shuffle below
  # seed: 42                         # --seed (with --shuffle 1)
  # n: 30                            # --end-index (--start-index 0)
  purpose: <one-line — diagnosis sample, pass rate here is a training metric>

mechanism_holdout:                   # 1.3 held-out sample — disjoint instances of the SAME
                                      # mechanism a fix claims to repair, not a fixed task range.
                                      # Policy only — the concrete registry (which mechanisms
                                      # have been enumerated, their task-id lists, which subject
                                      # models already consumed one) lives in OPTIMIZATION-LOG.md.
  task_split: train
  min_tasks: 15
  purpose: <one-line>

# --- Confidence ------------------------------------------------------
confidence:                          # 2.4 — omit entirely to use the adapter's stated defaults
  screen_num_trials: 1
  confirm_num_trials: 3
  credit_metric: episode_pass_rate   # or a paired statistical test, e.g. McNemar exact
  paired_arms: true                  # pair by TASK id, not episode — editing the agent
                                      # regenerates the episode, so episodes can't be held fixed

# --- Concurrency ------------------------------------------------------
concurrency:                         # --max-concurrency, split by provider mix since a proxied
  active: <n>                        # provider (e.g. Bedrock via a dev proxy) rate-limits far
  all_bedrock: <n>                   # below a direct provider (e.g. Fireworks) — measure both
  # note: <calls/min measured at each concurrency level, so this isn't reset from scratch>

# --- Stopping ------------------------------------------------------
stop_conditions:                     # generic-loop session inputs, not adapter facts —
  loop_budget: 5                     # cached here anyway since they're set once per session
  target: <e.g. mechanism_generalisation, or target_pass_rate: 0.90>
  credit_rule: <the precise statistical rule that credits a fix, if not a plain % threshold>
```

`stop_conditions` isn't part of the 7-subsection adapter contract — it's a
generic-loop input (`SKILL.md`'s Stop Conditions) — but it's cached in the
same file since a real session sets it once alongside the rest, not because
it's a tau-bench fact. Per `SKILL.md`'s precedence rule, anything you state
explicitly in the invocation still overrides the file.

### Where findings and cross-subject protocol live instead

Not in the yaml. Record these in `OPTIMIZATION-LOG.md` (add a section near
the top, before `## Trajectory`, if the file doesn't have one yet):

- **Scenario-invariant findings** — true regardless of subject model, e.g. a
  ground-truth task that's unexecutable by construction.
- **Subject-specific findings** — tied to one `subject_model.id`; tag with
  `measured_on: <model>` and treat as unverified for any other model until
  re-confirmed.
- **Mechanism-holdout registry** — one entry per mechanism a credited fix
  targets: the trigger, the population, the unseen task-id list, and which
  subject models have already consumed it as a holdout (an entry already
  used for one subject is still valid unseen for a different one).
- **On a subject-model swap** — `git checkout HEAD` the editable surface,
  re-baseline the tuning sample, open a new `OPTIMIZATION-LOG.md` section
  rather than appending, treat prior subject-specific findings as unverified,
  and either run the new subject in a fresh session with no access to the
  log/loop reports or pre-register the target mechanism before comparing to
  prior findings — the optimizer is a Claude Code session, and one that has
  read a previous model's logs is primed to re-find that model's mechanisms.
