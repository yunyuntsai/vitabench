# Adapter: tau2-bench

Fills the adapter contract required by `SKILL.md`,
organized into two blocks: **agent customization** (what's being optimized)
and **eval customization** (how to run it and turn results into a trustworthy
fix decision). Read the generic loop file first; this doc only answers "what
is true about tau2-bench specifically."

This is a separate benchmark from the legacy `tau-bench` adapter
(`ADAPTER-TAUBENCH.md`) in this same folder — it shares neither paths, nor
CLI, nor reward mechanics with that one. Derive every fact here from tau2's
own code; do not import a tau-bench path or flag by analogy.

Environment/auth setup (venv, API keys, proxy quirks) is **not** part of this
adapter — see `SETUP-TAU2-CRESTA-PROXY.md`.

---

## Block 1: Agent Customization

### 1.1 Agent Architecture

The "agent" is a generic LLM tool-calling loop, not a hand-written state
machine. Three agent implementations are registered
(`src/tau2/registry.py:297-308`), all in **`src/tau2/agent/llm_agent.py`**:

- `llm_agent` → `LLMAgent` (the default; `DEFAULT_AGENT_IMPLEMENTATION` in
  `src/tau2/config.py:15`). Half-duplex, turn-based tool caller. System prompt
  assembled at `llm_agent.py:78-82` from the `AGENT_INSTRUCTION` constant
  (`:24`) plus the domain policy. Loop entry: `generate_next_message` (`:105`)
  → `_generate_next_message` (`:115`) calls
  `generate(model=self.llm, tools=self.tools, messages=..., call_name="agent_response")`
  (`:128`) — `generate()` is the single litellm chokepoint in
  `src/tau2/utils/llm_utils.py:355-469`.
- `llm_agent_gt` → `LLMGTAgent` (`:166`) — additionally receives the expected
  resolution steps in the prompt (`SYSTEM_PROMPT_GT`, `:153`); gated by
  `LLMGTAgent.check_valid_task`. Not a normal optimization target.
- `llm_agent_solo` → `LLMSoloAgent` (`:321`) — no-user "ticket" mode; only tool
  calls, stops via an injected `done()` tool (`:357`) returning `STOP_TOKEN =
  "###STOP###"`. Selected via `--agent llm_agent_solo`.

A domain supplies, per `--domain`:

- **A system policy prompt** (the `<policy>` block injected into the agent
  system prompt). There is no single `wiki.md`; each domain ships policy
  markdown under `data/tau2/domains/<domain>/`:
  - airline → `data/tau2/domains/airline/policy.md`
    (`AIRLINE_POLICY_PATH`, `src/tau2/domains/airline/utils.py:5`; loaded
    `domains/airline/environment.py:26`).
  - retail → `data/tau2/domains/retail/policy.md`
    (`RETAIL_POLICY_PATH`, `domains/retail/utils.py:5`).
  - telecom (`--domain telecom`, manual tech-support) →
    `data/tau2/domains/telecom/main_policy.md` +
    `data/tau2/domains/telecom/tech_support_manual.md`
    (`domains/telecom/utils.py:8-9`).
  - telecom-workflow (`--domain telecom-workflow`, `policy_type="workflow"`) →
    `main_policy.md` + `data/tau2/domains/telecom/tech_support_workflow.md`
    (`utils.py:10-11`), concatenated as
    `<main_policy>...</main_policy><tech_support_policy>...</tech_support_policy>`
    at `domains/telecom/environment.py:124-133` (`domain_name="telecom-workflow"`
    at `:137`). Solo variants use `main_policy_solo.md` /
    `tech_support_workflow_solo.md` (`utils.py:13-18`).
  - banking_knowledge → **generated**, not a single file. `build_policy()` in
    `src/tau2/domains/banking_knowledge/retrieval.py:798` reads a
    per-retrieval-variant template from
    `data/tau2/domains/banking_knowledge/prompts/*.md` (e.g. `all_tools.md`,
    `classic_rag_qwen.md`, `agentic_search.md`, `grep_only.md`, `no_knowledge.md`,
    `full_kb.md`) with `{{component:NAME}}` includes from `prompts/components/*.md`
    (`PROMPTS_DIR`/`COMPONENTS_DIR` at `retrieval.py:55-56`; template list
    `:417-591`). Selected at run time by `--retrieval-config`.
- **Tool schemas + implementations** in one file per domain. Tools are methods
  on a `*Tools(ToolKitBase)` class; the **name, description, and parameter
  schema are parsed from each method's docstring** (`Tool` class,
  `src/tau2/environment/tool.py:38`, `_get_description` `:157`, arg parsing
  `:88-126`). So schema and implementation share one file:
  - airline → `src/tau2/domains/airline/tools.py` (`AirlineTools`, `:32`):
    `book_reservation`, `cancel_reservation`, `update_reservation_*`,
    `get_reservation_details`, `get_user_details`, `search_*_flight`,
    `send_certificate`, `transfer_to_human_agents`, `get_flight_status`,
    `calculate`, `list_all_airports`.
  - retail → `src/tau2/domains/retail/tools.py`.
  - telecom / telecom-workflow → `src/tau2/domains/telecom/tools.py` (agent)
    and `user_tools.py` (user-side).
  - banking_knowledge → composed by `build_tools()` (`retrieval.py:740`); base
    banking tools in `domains/banking_knowledge/tools.py` plus retrieval
    toolkits in `retrieval_toolkits.py` / `retrieval_mixins.py`; user tools
    `KnowledgeUserTools` in `tools.py`.
- **Mock domain data** (the DB the tools mutate): `db.json` (airline, retail,
  banking_knowledge, mock) or `db.toml` + `user_db.toml` (telecom), under
  `data/tau2/domains/<domain>/`. banking_knowledge additionally has the
  knowledge corpus `data/tau2/domains/banking_knowledge/documents/`.
- **Test tasks**: a hidden user instruction plus expected final
  actions / env-assertions / required communicated info. See 1.3 for where.
- **A simulated user**, itself an LLM (`UserSimulator`,
  `src/tau2/user/user_simulator.py:99`), which reveals information
  incrementally per the task's `user_scenario`. `DummyUser` (`:269`) is the
  no-op user used in solo mode.

Interaction loop per task (`src/tau2/orchestrator/orchestrator.py`,
`Orchestrator` `:350`, half-duplex): the env opens with a fixed agent greeting
`DEFAULT_FIRST_AGENT_MESSAGE` (`orchestrator.py:47`); the agent and simulated
user alternate, the agent emitting text and/or tool calls; tool calls execute
against the mock DB via `environment.get_response` / `_execute_tool_calls`
(`:313`); the episode ends on `###STOP###`, a user stop, `--max-steps`
(default `DEFAULT_MAX_STEPS = 200`, `config.py:4`), `--max-errors` (default 10,
`config.py:5`), or `--timeout`. Top-level driver:
`src/tau2/runner/simulation.py:run_simulation` (`:19`) calls
`orchestrator.run()` then **immediately `evaluate_simulation`** (`:76`) and
attaches `simulation.reward_info` (`:85`). Reward is computed once, at episode
end.

Reward is **not a single mechanism** — `evaluate_simulation`
(`src/tau2/evaluator/evaluator.py:88`) runs up to four sub-evaluators and the
final reward is the **product** of the components listed in
`task.evaluation_criteria.reward_basis` (`:223-256`):

1. **DB / ENV** (`EnvironmentEvaluator.calculate_reward`,
   `src/tau2/evaluator/evaluator_env.py:23`) — like tau-bench's hash compare,
   but applied to a *replayed* predicted environment. It rebuilds a **gold
   environment** by replaying `task.evaluation_criteria.actions`
   (`golden_actions`) via `gold_environment.make_tool_call(...)` (`:104-115`),
   rebuilds a **predicted environment** by replaying the agent's full
   trajectory (`set_state(..., message_history=full_trajectory)`, `:89-93`),
   then compares `get_db_hash()` **and** `get_user_db_hash()` of both
   (`:118-129`): `db_match = agent_db_match AND user_db_match`,
   `db_reward = 1.0 if db_match else 0.0`. Read-only tool calls (`get_*` /
   `find_*` / `list_*` / `calculate`) don't mutate the DB, so they cannot
   alone cause a hash mismatch — but a weak agent sprinkles extra lookups
   before the mutating call that matters, and a naive positional diff would
   mis-attribute the divergence to the lookup. The evaluator avoids this by
   comparing *final DB state*, not call sequence. Also runs `env_assertions`
   against the predicted env (`:134-148`).
2. **ACTION** (`evaluator_action.py:_check_actions`, `:16`) — for each
   expected `Action`, `gold_action.compare_with_tool_call(pred_tool_call)` is
   checked against every tool call in the trajectory (`:36`). An
   action-list/argument match, not a hash.
3. **COMMUNICATE** (`CommunicateEvaluator.calculate_reward`,
   `evaluator_communicate.py:13`) — checks each item in
   `task.evaluation_criteria.communicate_info` was conveyed to the user; each
   `communicate_check` carries an LLM-judged `justification`.
4. **NL_ASSERTIONS** (`evaluator_nl_assertions.py`) — LLM judge; WIP, only
   multiplies in if `NL_ASSERTION` is in `reward_basis`.

Premature termination (anything other than `AGENT_STOP` / `USER_STOP`) forces
reward `0.0` immediately (`evaluator.py:119-129`) with
`info.note = "Simulation terminated prematurely. Termination reason: <reason>"`.
No criteria → `1.0` (`:130-135`).

**This architecture claim is a hypothesis, not settled fact, until checked
against real evidence** — confirm it by reading at least one real passing
transcript and one real failing transcript (`simulations[<i>].messages` in
`results.json`) before relying on it in Step 1.2.

### 1.2 Editable Vs. Forbidden Surface

**Editable** — three co-equal surfaces. **Selection is root-cause-driven,
not ranking-driven** (per `SKILL.md` Step 1.3 / 2.1): edit the surface the
per-case evidence locates the root cause in. The list below is **not** a
priority order — policy markdown is **not** the default. Each surface has a
distinct root-cause class it's the right fix for:

1. **Policy markdown** (the `<policy>` block in the agent system prompt) — the
   files listed in 1.1:
   `data/tau2/domains/<domain>/policy.md` (airline, retail),
   `data/tau2/domains/telecom/main_policy.md` /
   `tech_support_manual.md` / `tech_support_workflow.md` (and `_solo`
   variants), and `data/tau2/domains/banking_knowledge/prompts/*.md` +
   `prompts/components/*.md` (selected by `--retrieval-config`). The target
   when the root cause is a missing or ambiguous **domain procedure or rule**
   the agent must follow (e.g., a troubleshooting step it skips, an
   eligibility condition it asserts without verifying, a domain-specific
   action ordering). **Includes the high-value "caves to user pressure on an
   ineligible action" class** — when the task intent (`description.purpose`)
   shows the task tests refusing a disallowed cancellation/refund/modification
   under user pressure and the agent performed it anyway, the fix is the
   domain eligibility rule plus an explicit refuse-under-pressure clause, here
   in policy — *not* a domain-agnostic "don't over-act" rule in the agent
   instruction (that misframe has produced no credit and a false "capability
   wall"). Frequently the target, but selected by the *motive* (Step 1.2) —
   not by default.
2. **Agent instruction text** wrapping the policy: the `AGENT_INSTRUCTION` /
   `SYSTEM_PROMPT` templates in `src/tau2/agent/llm_agent.py` (`:24-41`,
   `:138-163`, `:296-318`). The target when the root cause is a missing or
   ambiguous **domain-agnostic behavioral rule** that applies across the whole
   domain, not to one specific procedure (e.g., "execute the mutating call
   after confirmation", "transfer is terminal / complete actionable first",
   "don't loop / do each step once"). These belong in the instruction wrapper,
   not a domain policy file.
3. **Tool docstrings** (the LLM-visible name / description / parameter schema
   — parsed from the method docstring per `environment/tool.py:88-126`) in
   `src/tau2/domains/<domain>/tools.py` (+ `user_tools.py`,
   `retrieval_toolkits.py`, `retrieval_mixins.py`). The target when the root
   cause is an **ambiguous or wrong-argument tool call** — the schema the
   model sees misleads it (e.g., a parameter whose description lets the model
   pass the wrong ID). Clarify the docstring; do **not** re-state it in policy,
   which duplicates the schema and drifts from what the model actually sees.

A wrong-argument call is a docstring fix (3), not a policy fix (1); a missing
cross-domain behavioral rule is an agent-instruction fix (2), not a policy
fix (1). Editing policy to compensate for a docstring or instruction gap
duplicates guidance and drifts from the schema/text the model actually sees.

**Surface selection also has a prompt-perturbation footprint, distinct from
correctness.** The three surfaces are co-equal for *correctness* (use the one
the root cause lives in), but not for how many tasks an edit disturbs:

1. **Agent instruction** (`AGENT_INSTRUCTION`) is injected into **every
   domain's** system prompt — airline, retail, telecom, banking_knowledge
   (all use `SYSTEM_PROMPT` at `llm_agent.py:36-43`). An edit perturbs **all
   tasks in all domains**; you validate on one domain only, so cross-domain
   regressions are invisible. Highest footprint — use only when the root cause
   is genuinely domain-agnostic, and flag the cross-domain effect.
2. **Policy markdown** is per-domain — an edit perturbs **all tasks in one
   domain**. Medium footprint.
3. **Tool docstring** is seen only when the model elects to call that one
   tool — narrowest perturbation, lowest risk of regressing unrelated tasks.

When a root cause genuinely maps to more than one surface (a clarification
that could live in either a docstring or a policy/instruction rule), prefer
the **most localized** surface that still reaches the root cause: docstring >
policy > agent-instruction. This never overrides a clear root-cause mapping —
if the evidence points to one surface, use it — but it breaks ties and sizes
the edit. On weak or high-temperature subject models (adapter 2.4) prompt
fragility is severe enough that even a ~60-word, correctly-targeted policy
clause has been observed to regress unrelated 6-of-6 stable passers; in that
regime, minimality (Step 2.1) and unrelated-passer guards (Step 2.4) are not
optional. The concrete instances live in each run's `REVISION-LOG-loop<N>.md`.

**Forbidden** — editing these redefines what "correct" means for every task
that touches it; it does not fix the agent:

- **Tool method bodies** (the `invoke()` logic that mutates the DB and that
  the evaluator replays to build the predicted/gold environments) in
  `src/tau2/domains/<domain>/tools.py` and friends. The same file holds both
  surfaces — edit the docstring, never the body.
- **Ground-truth tasks** (expected actions, env-assertions, communicate_info,
  reward_basis, user_scenario):
  `data/tau2/domains/<domain>/tasks.json` (+ `split_tasks.json`,
  `tasks_voice.json`, `task_issues/`); banking_knowledge's
  `data/tau2/domains/banking_knowledge/tasks/task_*.json` (one file per task).
  Task model: `src/tau2/data_model/tasks.py`.
- **Reward / evaluation logic**: everything under `src/tau2/evaluator/`
  (`evaluator.py`, `evaluator_env.py`, `evaluator_action.py`,
  `evaluator_communicate.py`, `evaluator_nl_assertions.py`, `reviewer.py`,
  `hallucination_reviewer.py`, `review_llm_judge*.py`) and the reward data
  models in `src/tau2/data_model/simulation.py` (`RewardInfo`, `DBCheck`,
  `ActionCheck`, `EnvAssertionCheck`).
- **Mock environment DB / state** that the hash comparison runs against:
  `data/tau2/domains/<domain>/db.json` (airline/retail/banking/mock),
  `db.toml` + `user_db.toml` (telecom), the banking knowledge corpus
  `data/tau2/domains/banking_knowledge/documents/`, and
  `src/tau2/environment/*` (`environment.py: get_db_hash`,
  `get_user_db_hash`, `set_state`, `make_tool_call`, `run_env_assertion`;
  `db.py`, `toolkit.py`, `tool.py` execution wrappers).
- **Orchestration / termination / registry wiring** (defines the loop contract
  the evaluator assumes): `src/tau2/orchestrator/orchestrator.py`,
  `full_duplex_orchestrator.py`, `src/tau2/runner/simulation.py`,
  `src/tau2/registry.py`, `src/tau2/config.py`.

The **user-simulator surface** (`data/tau2/user_simulator/simulation_guidelines*.md`
and `src/tau2/user/user_simulator.py:SYSTEM_PROMPT`) is editable in principle
but is part of the measuring instrument — treat it as fixed unless the user
explicitly asks to change the benchmark itself (out of scope for this loop by
default), identical in spirit to tau-bench's "don't change the simulator"
rule.

### 1.3 Agent Environment (Scenario + Task Sampling)

- **Scenario selector:** `--domain {mock|airline|retail|telecom|telecom-workflow|banking_knowledge}`
  (`cli.py:53-59`). This selects the env package and therefore which
  policy/tools/tasks are in scope. `telecom-workflow` is a separate *domain*
  (workflow tech-support policy) sharing the telecom task corpus/splits.
- **Task set:** `--task-set-name` (`cli.py:104-110`), defaults to
  `config.domain` in `run_domain` (`src/tau2/runner/batch.py:875`). You almost
  never set it explicitly — `--domain` implies it. Registered sets: `mock`,
  `airline`, `retail`, `telecom`, `telecom_full`, `telecom_small`,
  `telecom-workflow`, `banking_knowledge`.
- **Task split:** `--task-split-name` (`cli.py:111-116`), default `"base"`.
  Splits live in `data/tau2/domains/<domain>/split_tasks.json` and partition
  task IDs:

  | domain | splits (name: count) | total tasks |
  |---|---|---|
  | mock | `base: 10` | 10 |
  | airline | `train: 30`, `test: 20`, `base: 50` | 50 |
  | retail | `train: 74`, `test: 40`, `base: 114` | 114 |
  | telecom / telecom-workflow | `small: 20`, `train: 74`, `test: 40`, `full: 2285`, `base: 114` | 2285 |
  | banking_knowledge | (none — `--task-split-name` ignored) | 97 |

  `base = train + test` for airline/retail/telecom. **There is no `dev`
  split** — only `train`/`test`/`base` (plus `small`/`full` for telecom).

- **Optimization sample:** the tasks used to discover clusters and choose
  fixes. Use `--task-split-name train` (optionally `--num-tasks N` to take the
  first N train tasks). There is **no `--shuffle`**; `--num-tasks N` always
  selects the same first-N tasks of the split in file order, so the sample is
  reproducible across runs given the same data. For an explicit list instead,
  `--task-ids <id ...>` (validated — every id must exist in the loaded split;
  it ANDs with the split).
- **Held-out sample:** a disjoint task sample used only for regression / final
  validation, never for choosing the fix. Use `--task-split-name test` — it is
  disjoint from `train` by construction (the split files partition task IDs),
  so no manual disjointness bookkeeping is needed. For telecom, prefer `test`
  (40) over `full` (2285) for the held-out unless you specifically want the
  long tail.
- **Reproducibility / seed:** `--seed` (`cli.py:159-164`, default
  `DEFAULT_SEED = 300`, `config.py:6`). The seed does **not** reorder tasks.
  In `run_tasks` (`src/tau2/runner/batch.py:517-518`) it seeds Python's RNG,
  which then emits **one seed per trial** (`seeds = [random.randint(0, 1e6) for
  _ in range(num_trials)]`); every task in trial `t` shares `seeds[t]`, which
  is forwarded to the provider via `set_seed` (`src/tau2/agent/base/llm_config.py:41-48`).
  With the default `temperature: 0.0` for both agent and user (`config.py:19-22`),
  a fixed seed makes a `(task, trial, seed)` tuple reproducible. The per-sim
  seed actually used is recorded in `simulations[*].seed`.

### 1.4 Architecture Escalation: per-task policy decoupling (GATED — outside the default 1.2 surface)

**Status: gated escalation, not a default optimization move.** This subsection is offline unless the
user explicitly opts in, because it edits the agent *architecture* (the policy loader), not the
agent's *knowledge*. Per `SKILL.md`'s "Architecture Escalation" section, three gates must hold:
(1) explicit user opt-in with the comparability caveat (results are a custom-agent measurement, **not**
benchmark/leaderboard-comparable), (2) **anti-leakage — the router uses user-visible signals only,
never `evaluation_criteria.actions` or any ground-truth field**, with an all-sections fallback, (3)
re-baseline after the change. Do not perform this during a normal content-editing loop; it is the
next step *after* content-editing hits a compounding-fragility ceiling.

**The wall it breaks.** tau2 loads the **whole** policy file into one `<policy>` block for every
task: `domains/<domain>/environment.py` does `policy = open(POLICY_PATH).read()` and passes it whole
as `domain_policy` to `SYSTEM_PROMPT.format(domain_policy=...)` (`llm_agent.py:80`). So splitting
`policy.md` into sections *in the file* does nothing — the loader still injects all sections into
every task. Decoupling requires editing the **loader**, which is architecture.

**Concrete plan (airline; same shape for retail/telecom with their own policy paths from 1.1/1.2):**

1. **Split** `data/tau2/domains/airline/policy.md` into topic files under
   `data/tau2/domains/airline/policy/`: `_base.md` (intro + book + general conduct + the Checked bag
   allowance table — cross-cutting, always loaded), `_cancel.md` (Cancel flight — Loop 1's
   verify-eligibility + stand-firm clauses live here), `_modify.md` (Modify flight),
   `_compensation.md` (Refunds and Compensation). **Keep `policy.md` intact** as the no-router
   fallback and the benchmark-comparable baseline path.
2. **Edit the loader** `src/tau2/domains/airline/environment.py:26` to load `_base.md` always + 1-2
   topic sections selected by a router. **Keep the original `fp.read()` path for `policy.md`**
   selectable (e.g. via an env var or a `--policy-mode {shared,routed}` flag) so the benchmark agent
   is unchanged and you can run both.
3. **Write the router** mapping `task.user_scenario.instructions.reason_for_call` /
   `task_instructions` keywords → sections: cancel/refund/compensation/insist/lenient → `_cancel` +
   `_compensation`; change/modify/flights/cabin → `_modify`; book/new reservation → `_base` (book
   rules). **Router uses ONLY `user_scenario` text — never `evaluation_criteria`.** Fallback: no
   confident match → load ALL sections (degrades to the shared-prompt agent, safe).
4. **Re-baseline** the routed agent on `--task-split-name train` → new baseline pass^3 (expect lower
   than the shared-prompt baseline initially).
5. **Resume the per-motive loop, now decoupled.** Each motive's fix edits *only its section file* →
   a `_cancel.md` edit never enters a compensation task's prompt → fixes accumulate **without
   compounding fragility** (the ceiling that capped the shared-prompt run at one fix is gone). The
   new bottleneck is router accuracy — measure mis-routing via the all-sections fallback rate.
6. **Final report — two labeled numbers:** (a) shared-prompt + content-optimized
   (benchmark-comparable, the ceiling) and (b) routed + section-optimized (custom architecture,
   non-comparable, whether decoupling lifted the ceiling). Never present (b) as a benchmark score.

**Forbidden even within this escalation:** routing on `evaluation_criteria` (ground truth) — that is
leakage and invalidates the run; and editing tool bodies / evaluator / tasks / DB (the 1.2 forbidden
list) — those remain forbidden regardless of escalation. The escalation widens the editable surface
from "policy text" to "policy text **+ the policy loader + a user-scenario router**," nothing else.

---

## Block 2: Eval Customization

### 2.1 Eval Invocation

Unlike tau-bench, **evaluation is built into the run**, not a separate pass.
`tau2 run` calls `evaluate_simulation` per task and embeds `reward_info` in
`results.json` (`runner/simulation.py:76-85`). There is no
`eval_optimize.py`-style wrapper that also clusters failures — the loop reads
`results.json` and derives cluster keys inline per the rule in 2.3 (a small
helper script may be written, but none ships).

Fresh run on a split (see 1.3 for why `train`/`test`, no shuffle):

```bash
tau2 run --domain airline \
  --agent-llm <model> --user-llm <model> \
  --task-split-name train --num-tasks 30 \
  --num-trials 1 --seed 300 --max-concurrency 3 \
  --save-to <run-name>
```

Fresh run on an explicit task list (optimization sample already chosen):

```bash
tau2 run --domain airline \
  --agent-llm <model> --user-llm <model> \
  --task-ids 3 7 12 \
  --num-trials 1 --seed 300 --save-to <run-name>
```

Model strings route via litellm's prefix convention straight into
`litellm.completion(model=...)` (`llm_utils.py:417-423`) — e.g.
`anthropic/glm-5.2`, `fireworks_ai/accounts/fireworks/models/deepseek-v4-flash`,
`openai/gpt-4.1-2025-04-14`, `vertex_ai/gemini-3...` (the last is special-cased
at `llm_utils.py:391-394` to set `VERTEXAI_LOCATION=global`). See
`SETUP-TAU2-CRESTA-PROXY.md` for which prefixes work in this environment.

Fast single-case rerun (Phase 2, Step 2.2) — one id, same models/seed:

```bash
tau2 run --domain airline \
  --agent-llm <model> --user-llm <model> \
  --task-ids <target_task_id> --num-trials 1 --seed 300 --save-to rerun_<id>
```

Re-analyze an existing `results.json` **without rerunning** (e.g. reusing a
prior loop's retest as this loop's baseline, or re-grading after task-def
changes):

```bash
# recompute reward_info from the embedded trajectories, write updated file:
tau2 evaluate-trajs data/simulations/<run>/results.json -o ./out_regrade
# ...or with current data-dir task defs instead of the embedded ones:
tau2 evaluate-trajs data/simulations/<run>/results.json --fresh-tasks -o ./out_regrade
# metrics only (no file written):
tau2 evaluate-trajs data/simulations/<run>/results.json
```

Evidence-gathering helpers (no rerun; for reading transcripts in Step 1.2 /
Step 2.3):

```bash
tau2 view --file data/simulations/<run>/results.json --only-show-failed   # browse failing transcripts
tau2 review data/simulations/<run>/results.json --mode user               # LLM fault isolation → user_only_review
```

`--verbose-logs` writes per-sim artifacts under
`<save_dir>/artifacts/task_<id>/sim_<id>/` (`task.log`, `llm_debug/*.json`
one per LLM call, and `sim_status.json` on infra failure — see 2.2). A default
run produces **only** `results.json`.

### 2.2 Result / Artifact Schema

`tau2 run` writes `data/simulations/<save-to>/results.json` (text mode =
monolithic JSON; `--save-to` omitted → auto-named
`<timestamp>_<domain>_<agent>_<user>`; if the file exists the run **resumes**
by default, skipping completed `(trial, task_id, seed)` tuples — `cli.py:151`,
`src/tau2/runner/checkpoint.py`, `batch.py:585-594`). Voice mode uses a
directory format; convert with `tau2 convert-results <path>`.

Top-level keys (verified against a real 50-task airline run):

- `info` — run config snapshot: `git_commit`, `num_trials`, `max_steps`,
  `max_errors`, `seed`, `agent_info` (`implementation`, `llm`,
  `llm_args.temperature`, `voice_settings`), `user_info` (`implementation`,
  `llm`, `llm_args.temperature`, `global_simulation_guidelines`,
  `persona_config`), `environment_info`, `retrieval_config`,
  `retrieval_config_kwargs`, ...
- `tasks` — list of full `Task` objects (embedded so `evaluate-trajs` can
  re-grade without the data dir). Each: `id`, `description`, `user_scenario`
  (`persona`, `instructions` {`domain`, `reason_for_call`, `known_info`,
  `unknown_info`, `task_instructions`}), `ticket`, `initial_state`, and
  **`evaluation_criteria`** — this is where expected actions/outputs live:
  `actions` (list of expected `Action`: `action_id`, `requestor`, `name`,
  `arguments`, `info`, `compare_args`), `env_assertions`,
  `communicate_info` (required info strings), `nl_assertions`, and
  `reward_basis` (e.g. `["DB","COMMUNICATE"]`).
- `simulations` — list of `SimulationRun`, one per `(task, trial)` actually
  executed.
- `simulation_index` — `null` in monolithic JSON (used by the dir format).

Per-simulation entry (`simulations[*]`, verified keys): `id`, `task_id`,
`timestamp`, `start_time`, `end_time`, `duration`, `termination_reason`,
`agent_cost`, `user_cost`, **`reward_info`**, **`messages`** (the full
message-by-message transcript — `list[Message]`; half-duplex), `ticks` (`null`
for half-duplex; full-duplex uses ticks instead), `trial`, `seed`, `mode`,
`review`, `user_only_review`, `info`, `auth_classification`,
`hallucination_retries_used`, `hallucination_check`, `provider_session_id`,
`policy`, `effect_timeline`.

Field mapping for the loop:

- **pass/fail + reward:** `reward_info.reward` (float; `1.0` = pass, `0.0` =
  fail; product of the `reward_basis` components). Pass/fail ≡
  `reward_info.reward == 1.0`.
- **full transcript / trajectory:** `messages` (`messages[*].tool_calls` on
  assistant messages; tool results are `role="tool"` messages).
- **task id:** `task_id` (string); cross-ref `tasks[*].id` for expected
  criteria.
- **expected actions / outputs:** the matching `tasks[*].evaluation_criteria`
  (`actions`, `communicate_info`, `env_assertions`, `nl_assertions`,
  `reward_basis`).
- **task intent / the WHY (mandatory Phase-1 evidence, per `SKILL.md` Step
  1.2):** `tasks[*].description` is a dict whose `purpose` field states, in
  the designer's words, what scenario the task tests (e.g. *"Testing that the
  agent refuses to proceed with a cancellation that is not allowed even if
  User mentions…"*, *"Check that agent doesn't cancel reservations if the
  refund is not applicable even if the user asks"*), plus `relevant_policies`/
  `notes`. `tasks[*].user_scenario.instructions.task_instructions` states the
  simulated user's scripted behavior (e.g. *"Do not take No for an answer"*
  or *"If the agent says it is not possible, insist that you are a silver
  member…"*). This is a **ground-truth WHY source** — the motive behind a
  wrong action (caves-to-pressure vs over-acts vs misread) is stated here, not
  recoverable from `reward_info` or tool calls alone. It is **subject-
  invariant** (the 30 tasks are identical across subject models), so reading
  it once pays off for every subject. Read it via
  `print('TASK',tid,'purpose:',str(t.get('description'))[:200])` plus the
  `task_instructions`. Do not diagnose a failure's WHY without it.
- **tool calls made:** `messages[*].tool_calls`.
- **failure cause (the WHAT):** `termination_reason` (see 2.3) and
  `reward_info.info` (`{env, nl, communicate, action}` plus a `note` string
  for premature termination / no-criteria cases). This is the mechanical
  WHAT; pair it with the task intent (the WHY) above before choosing a
  surface.

`reward_info` sub-structure (`RewardInfo`, `src/tau2/data_model/simulation.py`)
— verified keys: `reward`, `db_check` (`{db_match, db_reward}`),
`env_assertions` (`[{env_assertion, met, reward}]`),
`action_checks` (`[{action: {action_id, requestor, name, arguments, info,
compare_args}, action_match, action_reward, tool_type}]`),
`communicate_checks` (`[{info, met, justification}]`), `nl_assertions`,
`reward_basis` (list), `reward_breakdown` (dict basis → component reward, e.g.
`{"DB":1.0,"COMMUNICATE":0.0}`), `info` (`{env, nl, communicate, action}`).

A real failing entry (airline task 7, `termination_reason=user_stop`,
`reward_basis=["DB","COMMUNICATE"]`): DB and all `action_checks` passed;
one `communicate_check` failed:

```json
"reward_info": {
  "reward": 0.0,
  "db_check": {"db_match": true, "db_reward": 1.0},
  "action_checks": [{"action":{"action_id":"7_0","name":"get_reservation_details",...},
                     "action_match": true, "action_reward": 1.0, "tool_type": "read"}, ...],
  "communicate_checks": [{"info": "1628", "met": false,
                          "justification": "Information '1628' not communicated."}],
  "reward_basis": ["DB", "COMMUNICATE"],
  "reward_breakdown": {"DB": 1.0, "COMMUNICATE": 0.0},
  "info": {"env": null, "nl": null, "communicate": null, "action": null}
}
```

Verbose-mode artifacts (`--verbose-logs`), per sim under
`<save_dir>/artifacts/task_<id>/sim_<id>/`: `task.log` (DEBUG loguru sink),
`llm_debug/*.json` (one per LLM call; `--llm-log-mode all` keeps every call,
`latest` keeps only the most recent per call type — default `latest`,
`config.py:35`), and `sim_status.json` =
`{"status":"failed","reason":"infrastructure_error","error":...,"error_type":...}`
on infra failure (`batch.py:314-326`).

### 2.3 Failure Taxonomy

No LLM is required by default — cluster keys derive mechanically from
`reward_info` fields already present in `results.json` (a small helper may
compute them, but none ships). For each `SimulationRun` `s` with
`task = tasks[task_id]`:

1. **Premature termination bucket** (highest priority). If
   `s.termination_reason not in {"user_stop","agent_stop"}` → key =
   `TERM:<termination_reason>`. The `TerminationReason` enum
   (`src/tau2/data_model/simulation.py:1234-1244`) includes `max_steps`,
   `timeout`, `too_many_errors`, `agent_error`, `user_error`,
   `infrastructure_error`, `context_window_exceeded`, `unexpected_error` — all
   force reward `0.0` (`evaluator.py:119-129`). Fold in any
   `reward_info.info.note` starting with `"Simulation terminated prematurely"`.
   Cross-check `sim_status.json` (`reason="infrastructure_error"`) when
   `--verbose-logs` was on.
2. **Component-failure buckets** (when `termination_reason` is
   `user_stop`/`agent_stop` but `reward < 1`). Iterate the per-component check
   lists; emit one sub-key per failing check:
   - `DB` when `reward_info.db_check.db_match == False`.
   - `ENV_ASSERTION:<i>` for each `reward_info.env_assertions[i].met == False`
     (use the assertion text/id for granularity).
   - `ACTION_MISSED:<action_id>` for each
     `reward_info.action_checks[i].action_match == False` (use
     `action.action_id`, `action.name`, `action.tool_type`).
   - `COMM_MISSED:<info>` for each
     `reward_info.communicate_checks[i].met == False` (use the `info` string,
     e.g. `COMM_MISSED:1628`).
   - `NL_FAIL:<i>` for each failed `nl_assertions[i]`.
   - The cluster key = the sorted, `|`-joined tuple of failing sub-keys, e.g.
     `DB|COMM_MISSED:1628`; a single failure → that one sub-key.
   - `reward_info.reward_breakdown` alone gives a **coarse** key (which basis
     failed, e.g. `COMMUNICATE`); the per-check lists give the fine key. Prefer
     the fine key — two `COMMUNICATE` failures on different `info` strings are
     different root causes.
3. **`PASS`** when `reward == 1.0` (not a failure; excluded from clustering).

| Key prefix | Meaning | How it's derived | Actionable by default? |
|---|---|---|---|
| `TERM:<reason>` | Episode ended non-normally; reward forced 0 | `termination_reason` enum; `reward_info.info.note` | `infrastructure_error`/`context_window_exceeded`/`unexpected_error` → **No** (infra, 2.3.1); `too_many_errors`/`max_steps`/`timeout` → maybe (agent looped/stalled — read the transcript) |
| `DB` | Predicted DB state hash ≠ gold DB state hash after replay | `reward_info.db_check.db_match == False` | **Yes — and typically the largest cluster, so typically the priority.** `db_match == False` with all `action_checks` matched specifically means the agent made **extra** mutating calls beyond the expected set, or **wrong-argument** mutating calls that matched by name (`compare_args=null`) but produced the wrong state. The `DB` key collapses both; sub-cluster it before fixing (procedure below). Verify it isn't user-simulator drift (2.3.1). |
| `ENV_ASSERTION:<i>` | A predicted-env assertion predicate failed | `reward_info.env_assertions[i].met == False` | Yes — but read the assertion + transcript; some are borderline grading |
| `ACTION_MISSED:<action_id>` | An expected tool call was not made with matching args | `reward_info.action_checks[i].action_match == False` | Yes — usually a real agent bug; confirm the agent had the info to make the call |
| `COMM_MISSED:<info>` | A required info string was not conveyed to the user | `reward_info.communicate_checks[i].met == False` + `justification` | Yes — rarely non-actionable; the agent did the action but didn't tell the user |
| `NL_FAIL:<i>` | An NL-judge assertion failed | `reward_info.nl_assertions[i]` | Judge-dependent — treat as softer signal than the mechanical keys |

**Sub-clustering a residual `DB` bucket (mandatory before fixing *or*
deferring it).** `db_match == False` while every `action_check` matched means
the divergence lives in the *mutating* calls the action-checker doesn't fully
pin down (read-only `get_*`/`search_*`/`calculate` calls cannot cause a hash
mismatch on their own). For each `DB`-bucket case, compare the **expected**
mutating calls (`evaluation_criteria.actions` whose tool mutates the DB) against
the agent's **actual** mutating calls (`messages[*].tool_calls` to mutating
tools — `book_reservation`, `cancel_reservation`, `update_reservation_*`,
`send_certificate`, never the `get_*`/`search_*`/`calculate` reads). Classify
each case:

- **EXTRA** — the agent made a mutating call **not** in the expected set (e.g.
  cancelled a reservation the user didn't ask to cancel, issued an unrequested
  certificate). This is frequently the majority of a `DB` bucket, but "EXTRA"
  is a **WHAT**, not a WHY — the same extra cancel can come from very different
  motives with very different fixes. **Sub-classify EXTRA by motive, using the
  task intent** (`description.purpose` / `user_scenario.task_instructions`,
  per 2.2 and `SKILL.md` Step 1.2):
  - **EXTRA-under-pressure-on-ineligible** — the task's purpose is *"refuse a
    disallowed cancellation/refund even when the user pushes"* and the
    `task_instructions` script the user to insist/threaten/claim-status. The
    agent caved and performed an ineligible action. Fix: **policy** — the
    domain eligibility rule (e.g. tau2 airline cancellation eligibility: 24h
    / airline-cancelled / business / insured-covered) **plus** an explicit
    refuse-under-pressure clause. This is the high-value fixable class; do
    **not** misframe it as "over-acts" → behavioral/agent-instruction (that
    was a real misdiagnosis that produced no credit and a false "capability
    wall").
  - **EXTRA-speculative** — no user pressure; the agent acted unprompted (the
    user only asked a question / wanted information). Fix: domain-agnostic
    behavioral ("don't mutate without an explicit request") → agent-instruction
    or policy.
  - **EXTRA-misread-intent** — the agent thought the user requested the change
    but misread the request. Usually **capability** (intent-reading), not
    rule-fixable.
  The motive — not the extra call itself — picks the surface.
- **WRONG_ARGS** — the agent made the *expected* mutating call but with
  arguments that produced the wrong final state (wrong `reservation_id` /
  `cabin` / `payment_id` / flight set), surfacing as `DB` rather than
  `ACTION_MISSED` because `action_checks` matched by name with
  `compare_args=null`. Sub-classify by motive here too: a wrong *cabin* or
  *nonfree_baggages* that traces to an ambiguous schema is a **docstring** fix
  (e.g. clarifying how `nonfree_baggages` is computed from the policy's
  free-bag table); a wrong *reservation_id* after enumerating several is
  **capability** (wrong selection) and not rule-fixable.
- **MISSED_WRITE** — a mutating call was missed entirely but its
  `action_check` was absent or name-only; re-check whether it should have been
  an `ACTION_MISSED:<name>` key instead, and cluster it there.

Without this sub-cluster, a `DB` fix is a guess: EXTRA and WRONG_ARGS need
different surfaces, and within EXTRA the motive (under-pressure-on-ineligible
vs speculative vs misread) decides policy vs behavioral vs capability. Lumping
them hides a fixable majority behind a capability minority (or vice versa) —
and worse, misframing an EXTRA-under-pressure case as "over-acts" sends you to
the behavioral surface, yields no credit, and can look like a "capability wall"
when the real fix (policy eligibility + refuse-under-pressure) was never tried.
A `DB` bucket may legitimately turn out non-actionable (e.g. all WRONG_ARGS-
capability or EXTRA-misread), but only **after** this WHAT+WHY classification —
never from the `DB` label alone, and never from the WHAT alone. This is the
concrete "sub-cluster a large catch-all" + "derive the WHY" step `SKILL.md`
Step 1.2 requires.

**This taxonomy is a hypothesis, not settled fact, until checked against real
evidence.** The mechanical key is a pointer to a bucket of failures, not proof
they share one root cause. Always read `simulations[<i>].messages` (the full
transcript) for 1-2 representative cases before proposing a fix *or deferring
a cluster* from a cluster key alone — a `DB` mismatch and a `COMM_MISSED` can
both trace back to the user simulator never revealing a needed ID, which is
2.3.1 drift, not an agent policy gap; and conversely a `DB` bucket can be a
fixable EXTRA-write cluster hiding behind an uninformative label.

#### 2.3.1 Determining Actionability

A cluster is non-actionable — safe to defer or drop — only when it resolves to
one of these, **confirmed by evidence** (a transcript read, a clean rerun),
not merely because it looks like one:

- **Transient API / infra failure** — a `TERM:infrastructure_error` (or
  `TERM:context_window_exceeded`, `TERM:unexpected_error`) cluster caused by a
  provider outage/timeout, not agent logic. Confirm by rerunning clean
  (`--task-ids <id> --num-trials 1`); if it passes, it was infra.
- **User-simulator drift / hallucination** — the LLM user simulator failed to
  execute its own scripted `user_scenario` (skipped a conditional branch,
  volunteered the wrong ID, hallucinated a fact). tau2 has explicit tooling:
  `tau2 review --mode user` populates `simulations[*].user_only_review` with
  `source` ∈ {`user`,`agent`,`unknown`} and `severity` ∈
  {`critical_helped`,`critical_hindered`,`minor`} (`ReviewError`,
  `simulation.py:737-766`; backed by `src/tau2/evaluator/review_llm_judge_user_only.py`
  and `hallucination_reviewer.py`). `source=user` is a strong candidate signal
  — but still verify against the transcript before logging a cluster
  non-actionable. (`hallucination_retries_used` / `hallucination_check` track
  the full-duplex retry path.)
- **Evaluator gap** — a `reward_basis` entry whose corresponding check list is
  `null`/empty-with-`"No … to evaluate"` (`reward_info.info.note`, e.g.
  `"No communicate_info to evaluate"`; `evaluator.py:134`,
  `evaluator_env.py:58,265`). The component silently returns `1.0`, so a
  failure elsewhere may look isolated when the basis wasn't really exercised.
  Surface these as a smell, not a fix target.
- **Borderline / ambiguous grading** — the agent's answer is defensible but
  diverges from a narrowly-specified ground truth (e.g. a total that
  includes/excludes an edge-case component the grader didn't anticipate).
  Flag as a possible over-narrow grading case rather than adopting a fix that
  only chases the specific ground-truth value.

There is **no fully automatic fault-assignment classifier** that partitions a
0-reward into agent-vs-infra-vs-evaluator; `tau2 review` (LLM judge) is the
closest, and the mechanical key above is the deterministic alternative. Always
cite the specific evidence checked (which transcript, which divergence) in
`OPTIMIZATION-LOG.md` when tagging a cluster non-actionable — a
"non-actionable" tag with no cited evidence must be re-verified before it can
satisfy the generic loop's stop condition 2.

### 2.4 Confidence Policy

tau2 is **stochastic in structure, near-deterministic in default config**. The
user simulator is a sampled LLM call on every task
(`UserSimulator`, `src/tau2/user_simulator.py:99`), but both agent and user
default to `temperature: 0.0` (`config.py:19-22`) and the seed is forwarded to
the provider (`set_seed` → `llm_args["seed"]`,
`src/tau2/agent/base/llm_config.py:41-48`). So with defaults + a fixed
`--seed`, a `(task, trial, seed)` tuple is reproducible; variance re-enters
the moment any role raises temperature or the provider samples
non-deterministically server-side.

- **`--num-trials N`** (`cli.py:60-65`, default 1) runs each task N
  independent times (`batch.py:601-614`); each sim records `trial` and `seed`.
  `pass^k` metrics are computed over trials
  (`math.comb(success_count, k) / math.comb(num_trials, k)`,
  `src/tau2/metrics/agent_metrics.py:113-126`): `pass^1` = mean over tasks of
  (successes / N); `pass^k` = fraction of tasks where all k-of-N trial
  combinations succeed.
- **`--seed`** (default 300) seeds Python's RNG, which emits **one seed per
  trial** shared across all tasks in that trial (`batch.py:517-518`); it does
  **not** give per-task seeds (text mode) and does **not** reorder tasks.

- **Fast optimization loop (default):** `--num-trials 1 --seed 300` with
  `temperature: 0.0`. A single-trial pass/fail is sufficient evidence during
  the loop; don't pay for 3× trials on every iteration. (Caveat: a single
  trial is a point estimate, not confidence-grade — see credit threshold.)
- **Confidence mode (opt-in):** `--num-trials 3` when a result looks flaky, a
  failure is high-stakes, or before crediting a fix (the minimum to compute
  `pass^2`/`pass^3`); `--num-trials 5` for finer resolution and `pass^4`.
  Cost scales linearly (each trial = full re-run of the sample).
- **Credit metric & threshold:** report **`pass^1`** (mean per-task success
  across trials, computed by `compute_metrics` and exposed via
  `tau2 evaluate-trajs` / `tau2 leaderboard --metric pass_1`) as the primary
  credit metric. Pragmatic acceptance bar: **`pass^1 ≥ 0.80` on the held-out
  split with `--num-trials ≥ 3` and identical `--seed`**, re-graded with
  `tau2 evaluate-trajs --fresh-tasks` before reporting (avoids
  evaluator-definition drift). Single-trial `reward` is a point estimate, not
  a confidence-grade score.
- **Determinism sanity check:** for temp-0 models, trial variance should be
  ~0. If `pass^1` differs across trials at temp 0, suspect provider-side
  user-simulator non-determinism or infra errors — surface those via the
  `TERM:<reason>` bucket (2.3) rather than re-running blindly.

Used in Step 1.2 (how much to trust one failing transcript before clustering)
and Step 2.4 (how much to trust a retest before crediting a fix) of `SKILL.md`.

## Run Config (Optional)

`SKILL.md`'s Step 0 lets a run config file supply 1.3/2.1/2.4's inputs plus
the generic loop's stop conditions, instead of restating them in chat every
invocation. Configs live in `configs/<name>.yaml` in this same skill folder —
one file per (domain, sampling, model) combination you run repeatedly, e.g.
`configs/tau2-airline.yaml`.

**Three LLM roles, not one "model."** Conflating the agent under test with the
user simulator is how a run stops being comparable to anything measured before
— see `subject_model` / `simulator_model` below. Only `subject_model` is under
optimization; `simulator_model` should be treated as fixed (`hold_fixed: true`)
unless the user explicitly asks to change the benchmark itself.

The config holds only five sections — **roles, sampling, confidence,
concurrency, stopping**. It is not the place for accumulated results: any fact
discovered by running a loop (which subject models have already consumed the
`test` holdout, what's true only for one specific subject, a protocol for
swapping subjects) belongs in `OPTIMIZATION-LOG.md` instead (see `SKILL.md`'s
Artifact Conventions). Keeping findings there and only sampling *policy* here
is what lets the same yaml serve any subject model without silently carrying
over another model's results.

Schema (every field optional — anything omitted falls back to this adapter's
stated default, e.g. `--num-trials 1` for the fast loop):

```yaml
domain: airline                        # --domain

# --- Roles -------------------------------------------------------
subject_model:                         # the agent under test: --agent-llm
  id: fireworks_ai/accounts/fireworks/models/deepseek-v4-flash
  provider: fireworks_ai               # litellm prefix; routing is by prefix
  api_key_env: FIREWORKS_API_KEY       # env var litellm reads for this provider
  baselined: true|false                # false = no measurements exist yet for this subject
  selection_evidence: <one-line — why this model, what alternates it beat>

simulator_model:                       # the measuring instrument: --user-llm
  id: anthropic/glm-5.2                # via the Cresta Vertex proxy shim in SETUP-TAU2-CRESTA-PROXY.md
  provider: anthropic
  api_key_env: ANTHROPIC_API_KEY       # dummy via the shim
  hold_fixed: true                     # swapping this changes the benchmark, not the subject
  note: >                              # why it's fixed
    the user simulator's paraphrases decide whether required info is
    revealed/conveyed, so changing it changes winnability

optimizer: claude-code                 # reads results, writes fixes — not CLI-configurable;
                                       # it's the session running this skill

review:                                # optional 4th role (adapter 2.1's `tau2 review --mode user`), off by default
  enabled: false
  mode: user                           # populates user_only_review for 2.3.1 fault isolation
  model: <model>                       # --review-model (default DEFAULT_LLM_EVAL_USER_SIMULATOR)

# --- Sampling ------------------------------------------------------
tuning_sample:                         # 1.3 optimization sample — never used for regression checks
  task_split: train                    # --task-split-name
  task_ids: "0-29"                     # explicit --task-ids, or use num_tasks below
  # num_tasks: 30                      # --num-tasks (first N of the split, no shuffle)
  purpose: <one-line — diagnosis sample; pass rate here is a training metric>

mechanism_holdout:                     # 1.3 held-out sample — disjoint instances of the SAME
                                       # mechanism a fix claims to repair, not a fixed task range.
                                       # Policy only — the concrete registry (which mechanisms
                                       # have been enumerated, their task-id lists, which subject
                                       # models already consumed one) lives in OPTIMIZATION-LOG.md.
  task_split: test                     # disjoint from train by construction (split files partition ids)
  min_tasks: 15
  purpose: <one-line>

# --- Confidence ------------------------------------------------------
confidence:                            # 2.4 — omit entirely to use the adapter's stated defaults
  screen_num_trials: 1
  confirm_num_trials: 3
  credit_metric: pass_1                # tau2's pass^1; or a paired statistical test, e.g. McNemar exact
  paired_arms: true                    # pair by TASK id, not episode — editing the agent
                                       # regenerates the episode, so episodes can't be held fixed

# --- Concurrency ------------------------------------------------------
concurrency:                           # --max-concurrency, split by provider mix since a proxied
  active: <n>                          # provider (e.g. the Cresta Vertex shim) rate-limits far
  all_direct: <n>                      # below a direct provider (e.g. Fireworks) — measure both
  # note: <calls/min measured at each concurrency level, so this isn't reset from scratch>

# --- Stopping ------------------------------------------------------
stop_conditions:                       # generic-loop session inputs, not adapter facts —
  loop_budget: 5                       # cached here anyway since they're set once per session
  target: <e.g. mechanism_generalisation, or target_pass_1: 0.90>
  credit_rule: <the precise statistical rule that credits a fix, if not a plain pass^1 threshold>
```

`stop_conditions` isn't part of the 7-subsection adapter contract — it's a
generic-loop input (`SKILL.md`'s Stop Conditions) — but it's cached in the
same file since a real session sets it once alongside the rest. Per
`SKILL.md`'s precedence rule, anything you state explicitly in the invocation
still overrides the file.

### Where findings and cross-subject protocol live instead

Not in the yaml. Record these in `OPTIMIZATION-LOG.md` (add a section near the
top, before `## Trajectory`, if the file doesn't have one yet):

- **Domain-invariant findings** — true regardless of subject model, e.g. a
  ground-truth task that's unexecutable by construction, or an evaluator-gap
  smell (2.3.1) that affects every subject.
- **Subject-specific findings** — tied to one `subject_model.id`; tag with
  `measured_on: <model>` and treat as unverified for any other model until
  re-confirmed.
- **Mechanism-holdout registry** — one entry per mechanism a credited fix
  targets: the trigger, the population, the unseen `test`-split task-id list,
  and which subject models have already consumed it as a holdout (an entry
  already used for one subject is still valid unseen for a different one).
- **On a subject-model swap** — `git checkout HEAD` the editable surface (1.2),
  re-baseline the tuning sample, open a new `OPTIMIZATION-LOG.md` section
  rather than appending, treat prior subject-specific findings as unverified,
  and either run the new subject in a fresh session with no access to the
  log/loop reports or pre-register the target mechanism before comparing to
  prior findings — the optimizer is a Claude Code session, and one that has
  read a previous model's logs is primed to re-find that model's mechanisms.
