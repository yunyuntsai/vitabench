# Adapter: VitaBench

Fills the adapter contract required by `SKILL.md`, organized into two blocks:
**agent customization** (what's being optimized) and **eval customization**
(how to run it and turn results into a trustworthy fix decision). Read the
generic loop file first; this doc only answers "what is true about VitaBench
specifically."

VitaBench (`vita` CLI) adapted part of the tau2-bench codebase, but it is a
**separate benchmark** — it shares neither paths, nor CLI, nor reward mechanics
with `ADAPTER-TAU2BENCH.md` in this same folder. The two diverge most sharply
in three places this adapter accounts for:

1. **No per-domain policy.** tau2 ships a `policy.md` per domain and injects it
   into the agent prompt; VitaBench deliberately "eliminates domain-specific
   policies" (README:22) — one generic, domain-agnostic agent instruction is
   loaded for *every* domain. Domain knowledge lives in the **tools** and the
   **tasks**, not in a policy file. There is therefore **no "policy markdown"
   editable surface** here — the tau2 adapter's primary surface does not exist.
2. **LLM-judge reward, not a hash/action product.** tau2's reward is the product
   of DB-hash / action-match / communicate / NL components; VitaBench's reward
   is a **single NL-rubric judgement** — `1.0` iff every rubric is met, else
   `0.0` (`RewardType` has one member, `NL_ASSERTION`). The failure taxonomy is
   "which rubric did the LLM judge mark unmet," not "which DB hash diverged."
3. **One OpenAI-compatible endpoint, not litellm prefixes.** `generate()` POSTs
   to a single `base_url` from `models.yaml`; the `--*-llm` flags take a
   `models.yaml` `name`, not a `provider/model` string.

Derive every fact here from VitaBench's own code; do not import a tau2 path,
flag, or reward concept by analogy.

Environment/auth setup (venv, API keys, proxy quirks) is **not** part of this
adapter — a `SETUP-<environment>.md` in this folder, if one is added for the
current deployment, covers that. None ships yet.

---

## Block 1: Agent Customization

### 1.1 Agent Architecture

The "agent" is a generic LLM tool-calling loop, not a hand-written state
machine. Two agent implementations are registered
(`src/vita/registry.py:161,164`), both in **`src/vita/agent/llm_agent.py`**:

- `llm_agent` → `LLMAgent` (`llm_agent.py:32`; the default,
  `DEFAULT_AGENT_IMPLEMENTATION = "llm_agent"`, `config.py:91`). Half-duplex,
  turn-based tool caller. Its **entire** system prompt is
  `LLMAgent.system_prompt` (`llm_agent.py:56-64`) =
  `self.domain_policy.format(time=...)`, where `domain_policy` is the generic
  `agent_system_prompt` template (see below) — the **only** `{...}` placeholder
  is `{time}`. Loop entry: `generate_next_message` (`:89`) →
  `generate(model=self.llm, tools=self.tools, messages=..., enable_think=...)`
  (`:102`) — `generate()` is the single HTTP chokepoint in
  `src/vita/utils/llm_utils.py:221`.
- `llm_solo_agent` → `LLMSoloAgent` (`:126`) — no-user "ticket" mode; only tool
  calls, stops via `###STOP###` (`is_stop`, `:163`; `STOP_TOKEN` inherited from
  `BaseAgent`). System prompt = `prompts.solo_agent_system_prompt.format(time=...)`
  (`:152-160`). Selected via `--agent llm_solo_agent`. Not the default
  optimization target.

A domain supplies, per `--domain`:

- **A generic agent system prompt** — *not* a per-domain policy. Every domain's
  `get_environment` (`src/vita/domains/{ota,delivery,instore}/environment.py`)
  calls `get_agent_policy(language)` (`src/vita/environment/environment.py:26`),
  which returns `prompts.agent_system_prompt` — the **same**
  `src/vita/prompts/agent_system_prompt.yaml` for `delivery`, `instore`, `ota`,
  and the comma-joined cross domain (`get_cross_environment`,
  `environment.py:350`). It is a short domain-agnostic instruction: tool-use
  discipline ("ask for missing params, else extract them", "use Precondition /
  Postcondition"), conversation discipline ("don't fabricate", "don't
  divergently upsell", "end with `###STOP###`"), plus `{time}`. There is **no**
  `policy.md`, no `wiki`, no domain rule file loaded into the prompt. This is
  the headline architectural fact: the only prompt the agent ever sees (besides
  the tool schemas) is this one generic instruction.
- **Tool schemas + implementations**, split across two files per domain:
  - **Bodies** in `src/vita/domains/<domain>/tools.py` — methods on a
    `*Tools(ToolKitBase)` class decorated with `@is_tool(...)` (e.g. OTA's
    `get_ota_hotel_info`, `create_ota_order`, `cancel_ota_order`, ...). The
    method **name** and the parameter **names/types/required-ness** come from
    the Python signature here.
  - **Descriptions** in `src/vita/domains/<domain>/tools_schema.py` —
    `TOOL_DESCRIPTIONS_ZH` / `TOOL_DESCRIPTIONS_EN` dicts keyed by tool name,
    each holding `description`, `preconditions`, `postconditions`, `args`
    (per-arg descriptions), `returns`, `tool_type` (`READ`/`WRITE`/`GENERIC`).
    A `ToolSchemaManager` (`src/vita/utils/schema_utils.py:160`) is created per
    domain via `create_tool_schema_manager`.
  - **Wiring:** the `ToolKitBase` metaclass (`src/vita/environment/toolkit.py`,
    the `__init_subclass__` block around `:55-112`) calls
    `generate_tool_docstring(domain, tool_name, language)`
    (`schema_utils.py:52`) for every `@is_tool` method and **overwrites
    `method.__doc__`** with a synthesized docstring
    (`schema_utils.py:81-95`: `description` + `Preconditions:` + `Postconditions:`
    + `Args:` + `Returns:`). The `Tool` class then parses *that synthesized
    docstring* + the Python signature into the OpenAI function schema
    (`Tool.parse_data`, `src/vita/environment/tool.py:80`; `openai_schema`
    `:128`). **Consequence:** the raw docstrings written in `tools.py` are
    overwritten at class-creation time — editing them is futile. The
    LLM-visible tool text is authored in `tools_schema.py`.
- **Mock domain data** (the DB the tools read/mutate): embedded *per-task* in
  `task.environment` (see 1.3 / 2.2) — `orders`, `hotels`/`attractions`/
  `flights`/`trains` (OTA), `user_id`, `time`, `weather`, `location`,
  `user_historical_behaviors`. There is no standalone `db.json` file; the DB is
  part of each task definition.
- **Test tasks**: a user instruction + user persona + NL rubrics + expected
  orders. See 1.3 / 2.2.
- **A simulated user**, itself an LLM (`UserSimulator`,
  `src/vita/user/user_simulator.py:38`), which plays `task.user_scenario.user_profile`
  and pursues `task.instructions`, revealing information incrementally.
  `DummyUser` (`:164`) is the no-op user for solo mode. The user stops the
  episode by emitting `STOP` / `TRANSFER` / `OUT_OF_SCOPE` in its message
  (`UserSimulator.is_stop`, `:93-105`; constants in `src/vita/user/base.py`).

Interaction loop per task (`src/vita/orchestrator/orchestrator.py`,
`Orchestrator` `:46`, half-duplex): the env opens with a fixed agent greeting
`get_default_first_agent_message` (`:35`; "你好，请问需要什么服务？" /
"Hello, how can I help you?"). Agent and simulated user alternate, the agent
emitting text and/or tool calls; tool calls execute against the per-task DB via
`environment.get_response` (`environment.py:235`) / `make_tool_call` (`:108`).
The episode ends on a user `STOP`/`TRANSFER`/`OUT_OF_SCOPE`, an agent
`###STOP###`, `--max-steps` (default `DEFAULT_MAX_STEPS = 300`, `config.py:79`),
`--max-errors` (default `DEFAULT_MAX_ERRORS = 10`, `config.py:81`), or
`INVALID_AGENT_MESSAGE` (the agent emitted 3 consecutive messages with neither
text content nor a tool call, `orchestrator.py:289-314`). Top-level driver:
`run_domain` (`src/vita/run.py:108`) → `run_tasks` (`:179`) → `run_task`
(`:446`, which retries up to 3× on any exception, `:492-520`) →
`_run_task_internal` (`:523`) calls `orchestrator.run()` then **immediately
`evaluate_simulation`** (`:613`) and attaches `simulation.reward_info`
(`:622`). Reward is computed once, at episode end.

Reward is **a single LLM-judged NL-rubric score**, dispatched on
`evaluation_type` (`evaluate_simulation`, `src/vita/evaluator/evaluator.py:6`):

- **Premature termination** (`termination_reason ∈ {TOO_MANY_ERRORS, MAX_STEPS,
  INVALID_AGENT_MESSAGE}`) → `RewardInfo(reward=0.0, info={"note": "Simulation
  terminated prematurely..."})` immediately (`evaluator.py:18-28`). Note the
  enum (`src/vita/data_model/simulation.py:314-319`) has **no**
  `infrastructure_error` / `timeout` / `context_window_exceeded` / `agent_error`
  values — infra failures are not a termination reason here (see 2.3.1 for how
  they actually surface).
- **No evaluation criteria** → `reward=1.0` (`evaluator.py:29-33`).
- Otherwise, one of four `TrajectoryEvaluator` modes
  (`src/vita/evaluator/evaluator_traj.py:15`), all **LLM-judge**:
  - `trajectory` (default, `DEFAULT_EVALUATION_TYPE`, `config.py:88`) —
    **sliding window**: `window_size=10`, `overlap=2` (`:25-26`); rubric states
    are carried across windows and may flip false→true or true→false
    (`:240-249`); `reward = 1.0` iff **all** rubrics met (`:88-90`).
  - `trajectory_full_traj_rubric` (`:334`) — whole trajectory in one judge call,
    rubric-granular.
  - `trajectory_sliding_wo_rubric` (`:464`) — sliding window with a carried
    `memory`, but a **single overall** pass/fail (no per-rubric breakdown).
  - `trajectory_full_traj_wo_rubric` (`:624`) — whole trajectory, single overall
    pass/fail.
- The judge prompt templates are `src/vita/prompts/sliding_window_eval_template.yaml`,
  `full_trajectory_eval_template.yaml`, `sliding_window_eval_no_rubrics_eval_template.yaml`,
  `full_trajectory_no_rubrics_eval_template.yaml` (bilingual zh/en). Their
  stated rules matter for actionability (2.3.1): **query-tool returns don't
  count** — only the assistant's user-facing reply does; **user compromise
  doesn't satisfy** strict rubrics (quantity/delivery-time must be exact);
  **functional equivalence** applies to addresses/notes; an order rubric is unmet
  if the tool call failed even when the agent believed it succeeded, or if the
  user said they'd order themselves.
- Rubrics come from `task.evaluation_criteria`:
  `expected_states[*].state_rubrics` (per-state NL strings) +
  `overall_rubrics` (task-level NL strings), de-duplicated and indexed as
  `rubric_0..rubric_{n-1}` (`evaluator_traj.py:101-139`). The
  `expected_states` also carry `required_orders` / `optional_orders` (`Order`
  objects, `data_model/tasks.py:458-485`), but the trajectory evaluator does
  **not** hash-compare the DB and does **not** execute `env_assertions` — it
  only feeds the NL rubrics (which themselves encode the expected order
  details) to the judge. `RewardType` has exactly one member, `NL_ASSERTION`
  (`tasks.py:123`); `reward_breakdown = {NL_ASSERTION: rubric_score}`
  (fraction of rubrics met). The tau2 `reward_basis` product mechanics do not
  exist here.

**This architecture claim is a hypothesis, not settled fact, until checked
against real evidence** — confirm it by reading at least one real passing
transcript and one real failing transcript (`simulations[<i>].messages` +
`reward_info.nl_rubrics` in `results.json`) before relying on it in Step 1.2.
Pay extra attention to the judge's `justification` text — it is the closest
thing to a per-rubric root-cause hint the artifact gives you, and it is itself
an LLM opinion, not ground truth.

### 1.2 Editable Vs. Forbidden Surface

**Editable** — two co-equal surfaces (there is no third "policy markdown"
surface; the benchmark ships none). **Selection is root-cause-driven, not
ranking-driven** (per `SKILL.md` Step 1.3 / 2.1): edit the surface the
per-case evidence locates the root cause in. The list below is **not** a
priority order. Each surface has a distinct root-cause class it's the right fix
for:

1. **Agent instruction text** — `src/vita/prompts/agent_system_prompt.yaml`
   (bilingual `chinese` / `english` blocks). This is the **only** prompt the
   `LLMAgent` sees besides tool schemas, and it is the **entire** domain-agnostic
   behavior spec. The target when the root cause is a missing or ambiguous
   **domain-agnostic behavioral rule** that applies across the whole benchmark —
   e.g. "confirm before placing a mutating order", "re-state the order summary
   and total to the user before `###STOP###`", "don't claim success without
   checking the tool result", "ask for the missing ID rather than guessing",
   "end the conversation with `###STOP###` once the user has no further need".
   These belong here (the agent-instruction wrapper), not in a tool docstring.
   **Note the `Precondition` / `Postcondition` references** in the instruction
   text — the agent is told to follow tool pre/postconditions, so a
   precondition the agent ignores may be a *docstring* gap (surface 2) rather
   than an instruction gap; check which the evidence points to.
2. **Tool schema descriptions** — the `TOOL_DESCRIPTIONS_ZH` /
   `TOOL_DESCRIPTIONS_EN` dicts in
   `src/vita/domains/<domain>/tools_schema.py` (the `description`,
   `preconditions`, `postconditions`, `args`, `returns` per tool). This is the
   LLM-visible tool text (the metaclass overwrites `tools.py` docstrings from
   it, per 1.1). The target when the root cause is an **ambiguous or
   wrong-argument tool call** — the description/arg-text the model sees
   misleads it (e.g. an arg whose description lets the model pass the wrong ID,
   a `preconditions` clause that fails to say "call `get_*_info` first to obtain
   the ID", a `postconditions` clause that doesn't state the order is only
   placed on success). Clarify the dict entry; do **not** re-state it in the
   agent instruction, which duplicates the schema and drifts from what the model
   actually sees at call time. Editing the raw docstring in `tools.py` is
   futile — it is overwritten.

A wrong-argument call is a tools_schema fix (2), not an agent-instruction fix
(1); a missing cross-domain behavioral rule is an agent-instruction fix (1),
not a tools_schema fix (2). Editing the agent instruction to compensate for a
docstring gap duplicates guidance and drifts from the per-tool text the model
actually sees.

**Surface selection also has a prompt-perturbation footprint, distinct from
correctness.** The two surfaces are co-equal for *correctness* (use the one the
root cause lives in), but not for how many tasks an edit disturbs:

1. **Agent instruction** (`agent_system_prompt.yaml`) is injected into **every
   task of every domain** — `delivery`, `instore`, `ota`, and the cross domain
   all load the same file (`get_agent_policy` is domain-agnostic, 1.1). An edit
   perturbs **all tasks in all domains**; you validate on one domain only, so
   cross-domain regressions are invisible. **Highest footprint** — use only when
   the root cause is genuinely domain-agnostic, and flag the cross-domain
   effect. (This is the same shape as tau2's `AGENT_INSTRUCTION` footprint, but
   worse here: tau2 at least has per-domain policy as a more-localized
   alternative; VitaBench does not — see 1.4.)
2. **Tool schema description** (`tools_schema.py`) is seen only when the model
   elects to call that one tool — **narrowest** perturbation, lowest risk of
   regressing unrelated tasks. When a root cause genuinely maps to both
   surfaces (a clarification that could live in either a tool `preconditions`
   clause or an agent-instruction rule), prefer the **more localized** surface
   that still reaches the root cause: **tools_schema > agent-instruction**.
   This never overrides a clear root-cause mapping — if the evidence points to
   one surface, use it — but it breaks ties and sizes the edit. On weak or
   high-temperature subject models (adapter 2.4) prompt fragility is severe;
   minimality (`SKILL.md` Step 2.1) and unrelated-passer guards (Step 2.4) are
   not optional. The concrete instances live in each run's
   `REVISION-LOG-loop<N>.md`.

**Forbidden** — editing these redefines what "correct" means for every task
that touches it; it does not fix the agent:

- **Tool method bodies** (the `@is_tool`-decorated functions' logic that
  reads/mutates the per-task DB) in `src/vita/domains/<domain>/tools.py`, and
  the **parameter signatures** the OpenAI schema is built from
  (`Tool.parse_data` reads `sig.parameters`). The same `tools.py` file holds
  the implementation — edit the `tools_schema.py` description, never the body
  or signature. (Renaming/re-typing an arg changes the schema *and* the
  implementation contract; it is not a docstring fix.)
- **Ground-truth tasks**: `data/vita/domains/<domain>/tasks.json` and
  `tasks_en.json` (selected by `--language`), and
  `data/vita/domains/cross_domain/tasks.json` / `tasks_en.json`. Each task
  carries `instructions`, `user_scenario.user_profile`, `environment` (the DB),
  and `evaluation_criteria` (`expected_states` with `required_orders` /
  `optional_orders` / `state_rubrics`, plus `overall_rubrics`). Task model:
  `src/vita/data_model/tasks.py`.
- **Reward / evaluation logic**: everything under `src/vita/evaluator/`
  (`evaluator.py`, `evaluator_traj.py`, `evaluator_base.py`) and the reward data
  models in `src/vita/data_model/simulation.py` (`RewardInfo`, `NLRubricCheck`,
  `TerminationReason`, `EvaluationType`) and `src/vita/data_model/tasks.py`
  (`RewardType`, `EvaluationCriteria`, `ExpectedState`, `Action`, `EnvAssertion`).
  This **includes the eval prompt templates**
  `src/vita/prompts/*eval_template*.yaml` — they define what the judge asks and
  the strictness rules (query-results-don't-count, no-user-compromise,
  functional-equivalence); editing them redefines "correct."
- **Mock environment DB / state**: the per-task `environment` block embedded in
  `tasks.json` (orders/hotels/attractions/flights/trains/weather/location/
  user_historical_behaviors/time), `src/vita/environment/*`
  (`environment.py`: `get_agent_policy`, `get_cross_environment`,
  `make_tool_call`, `run_env_assertion`, `get_db_hash`; `db.py`, `toolkit.py`,
  `tool.py` execution wrappers), and the domain data models + `get_environment`
  in `src/vita/domains/<domain>/{data_model,environment}.py`.
- **Orchestration / termination / registry wiring** (defines the loop contract
  the evaluator assumes): `src/vita/orchestrator/orchestrator.py`,
  `src/vita/run.py`, `src/vita/registry.py`, `src/vita/config.py`.

The **user-simulator surface** (`src/vita/prompts/user_system_prompt.yaml` and
`dummy_user_system_prompt.yaml`, and `src/vita/user/user_simulator.py`) is
editable in principle but is part of the measuring instrument — the simulated
user's paraphrases decide whether required info is revealed and whether a
conveyed-fact rubric is satisfied, so changing it changes **winnability**, not
the agent. Treat it as fixed unless the user explicitly asks to change the
benchmark itself (out of scope for this loop by default), identical in spirit to
tau2's "don't change the simulator" rule. The **evaluator LLM** (`--evaluator-llm`,
`DEFAULT_LLM_EVALUATOR = "anthropic.claude-3.7-sonnet"`, `config.py:95`) is
likewise the measuring instrument — see 2.4: swapping it changes the reward
signal and is not comparable to a prior run.

### 1.3 Agent Environment (Scenario + Task Sampling)

- **Scenario selector:** `--domain {delivery|instore|ota|delivery,instore,ota}`
  (`cli.py:27-32`, default `delivery,instore,ota` — i.e. **cross-domain by
  default**). A comma-joined value selects the cross environment
  (`get_cross_environment`, `run.py:550`; tools from all listed domains merged
  into one `ToolKitBase`, `environment.py:284-352`) and the `cross_domain` task
  loader (`run.py:58-60`). A single domain selects that domain's env + tasks.
  The README's "main results" are the 100 cross-scenario tasks; the 300
  single-scenario tasks are 100 per domain.
- **Task set:** `--task-set-name` (`cli.py:78-83`, choices from the registry:
  `delivery`, `instore`, `ota`, `cross_domain`, `registry.py:162-169`). Defaults
  to `config.domain` in `run_domain` (`run.py:122-125`) — so `--domain` implies
  it and you almost never set `--task-set-name` explicitly.
- **Task IDs:** `--task-ids <id ...>` (`cli.py:84-89`, string `nargs="+"`).
  Validated — every id must exist in the loaded set, else `ValueError`
  (`run.py:82-86`). IDs are **opaque strings** like `"D0812006"` (delivery) or
  `"D0812006"`-style codes, **not** sequential integers — do not assume
  `0..N-1`. For an explicit list, pass the exact string IDs.
- **`--num-tasks N`** (`cli.py:90-95`) takes the **first N** tasks of the loaded
  set in file order (`run.py:88`). There is **no `--shuffle`**; the sample is
  reproducible across runs given the same data + `--language`.
- **Splits:** there is **no train/test/dev split mechanism**. Each domain has
  one flat task file: `data/vita/domains/<domain>/tasks.json` (Chinese, 100
  tasks/domain; `cross_domain` 100) and `tasks_en.json` (English). So the
  optimization vs. held-out partition is **your own manual bookkeeping**:
  pick an optimization sample (e.g. `--num-tasks 70` or an explicit
  `--task-ids` list) and a **disjoint** held-out `--task-ids` list for
  regression / final validation. Record both ID lists in `OPTIMIZATION-LOG.md`
  (the generic loop's mechanism-holdout registry) — unlike tau2, nothing in the
  benchmark enforces disjointness for you.
- **Optimization sample:** the tasks used to discover clusters and choose
  fixes. Use `--num-tasks N` (first N) or `--task-ids <list>` on one domain
  (prefer a single domain first — the cross domain's 100 tasks span all three
  toolkits and are the hardest, per README: 32.5% ceiling).
- **Held-out sample:** a disjoint task-id list used only for regression / final
  validation, never for choosing the fix. Pass it as `--task-ids <...>`; verify
  no overlap with the optimization sample by hand.
- **Reproducibility / seed:** `--seed` (`cli.py:139-144`, default
  `DEFAULT_SEED = 300`, `config.py:82`). In `run_tasks` (`run.py:242-244`) it
  seeds Python's RNG, which emits **one seed per trial**
  (`seeds = [random.randint(0, 1000000) for _ in range(num_trials)]`); every
  task in trial `t` shares `seeds[t]`, forwarded to agent **and user** via
  `set_seed` → `llm_args["seed"]` (`llm_agent.py:114-121`,
  `user_simulator.py` `BaseUser.set_seed`). With the default `temperature: 0.0`
  (`configs/models.yaml`), a `(task, trial, seed)` tuple is reproducible for the
  agent + user roles. The seed does **not** reorder tasks and does **not** seed
  the evaluator LLM (see 2.4). The per-sim seed actually used is in
  `simulations[*].seed`.
- **Language:** `--language {chinese|english}` (`cli.py:166-172`, default
  `chinese`, `config.py:87`). It selects **both** the task file
  (`tasks.json` vs `tasks_en.json`, `utils.py:27-35`) **and** the prompt
  templates' language block. A run's language is part of its identity — do not
  compare a Chinese run to an English run.

### 1.4 Architecture Escalation: per-domain policy injection (GATED — outside the default 1.2 surface)

**Status: gated escalation, not a default optimization move.** This subsection
is offline unless the user explicitly opts in, because it edits the agent
*architecture* (the prompt loader), not the agent's *knowledge*. Per
`SKILL.md`'s "Architecture Escalation" section, three gates must hold:
(1) explicit user opt-in with the comparability caveat (results are a
custom-agent measurement, **not** benchmark/leaderboard-comparable), (2)
**anti-leakage — any routing uses user-visible signals only, never
`evaluation_criteria` / `expected_states` / rubrics / `required_orders`**, with
an all-policy fallback, (3) re-baseline after the change. Do not perform this
during a normal content-editing loop; it is the next step *after*
content-editing hits a compounding-fragility ceiling.

**The wall it breaks.** VitaBench loads the **same** generic
`agent_system_prompt.yaml` for every domain (`get_agent_policy`,
`environment.py:26`). Because there is no per-domain policy file at all, the
default architecture offers **no localized surface between "one agent
instruction for all tasks" and "one tool docstring for one tool."** When every
domain-agnostic behavioral fix must go in the shared instruction (1.2 surface
1), every edit perturbs all tasks in all domains — the ceiling is reached
fast, and there is no per-domain escape hatch short of editing the loader. A
per-domain policy block is therefore the structural decoupling step here
(analogous to tau2's per-task policy decoupling, but coarser — per-domain, since
that is the granularity the loader naturally supports).

**Concrete plan:**

1. **Author** a per-domain policy markdown (e.g.
   `data/vita/domains/ota/policy_zh.md` / `policy_en.md`) — domain procedures
   the agent must follow (OTA: hotel-selection rules, order-placement
   confirmation, cancellation eligibility). Keep the generic
   `agent_system_prompt.yaml` intact as the no-loader baseline and the
   benchmark-comparable path.
2. **Edit the loader** `src/vita/environment/environment.py:26` (`get_agent_policy`)
   — or each domain's `get_environment` (`policy=get_agent_policy(language)` call)
   — to load the domain policy file and **prepend** it to `agent_system_prompt`
   before `.format(time=...)`. Keep the original generic-only path selectable
   (e.g. via an env var or a `--policy-mode {shared,routed}` flag) so the
   benchmark agent is unchanged and you can run both.
3. **Router is per-domain, not per-task** here: the domain is already known at
   env construction (`--domain`), so no LLM/keyword routing is needed — select
   the domain's policy file directly. If you later want *within-domain* section
   routing (e.g. OTA hotel vs. flight vs. attraction sections), the router must
   use **only** `user_scenario.user_profile` / the live conversation / the
   user's stated need — **never** `evaluation_criteria`, `expected_states`, or
   rubrics. Fallback: no confident match → load ALL sections (degrades to the
   full-domain-policy agent, safe).
4. **Re-baseline** the routed agent on the optimization sample → new baseline
   `pass^1` (expect lower than the generic-prompt baseline initially — the
   added policy is new context the model hasn't been tuned against).
5. **Resume the per-motive loop, now decoupled.** A domain-A policy edit no
   longer enters domain-B tasks' prompts → fixes accumulate **without
   compounding fragility**.
6. **Final report — two labeled numbers:** (a) generic-prompt + content-optimized
   (benchmark-comparable, the ceiling) and (b) per-domain-policy + content-optimized
   (custom architecture, non-comparable, whether decoupling lifted the ceiling).
   Never present (b) as a benchmark score.

**Forbidden even within this escalation:** routing on `evaluation_criteria`
(ground truth) — that is leakage and invalidates the run; and editing tool
bodies / signatures / evaluator / tasks / DB (the 1.2 forbidden list) — those
remain forbidden regardless of escalation. The escalation widens the editable
surface from "agent instruction text" to "agent instruction text **+ the
policy loader + per-domain policy markdown files**," nothing else.

---

## Block 2: Eval Customization

### 2.1 Eval Invocation

Like tau2, **evaluation is built into the run**, not a separate pass.
`vita run` calls `evaluate_simulation` per task and embeds `reward_info` in the
results JSON (`run.py:613-622`). There is no clustering wrapper that ships —
the loop reads the results JSON and derives cluster keys inline per the rule in
2.3 (a small helper script may be written, but none ships).

Fresh run on an explicit task list (optimization sample already chosen — note
the string IDs):

```bash
vita run --domain ota \
  --agent-llm <model> --user-llm <model> --evaluator-llm <model> \
  --task-ids D0812006 D0812007 \
  --num-trials 1 --seed 300 --max-concurrency 1 \
  --language chinese --evaluation-type trajectory \
  --save-to <run-name>
```

Fresh run taking the first N tasks of a domain (no shuffle, file order):

```bash
vita run --domain ota \
  --agent-llm <model> --user-llm <model> --evaluator-llm <model> \
  --num-tasks 30 --num-trials 1 --seed 300 \
  --language chinese --save-to <run-name>
```

**Model strings are `models.yaml` names, not litellm prefixes.** `generate()`
(`llm_utils.py:221`) POSTs OpenAI-format JSON to a **single** `base_url` taken
from `models.yaml`'s `default.base_url` (`llm_utils.py:209-211`), with the body's
`model` field = the `name` of the chosen entry (`data.update(models[model])`,
`llm_utils.py:~258`). So `--agent-llm` / `--user-llm` / `--evaluator-llm` each
take a `name` from `models.yaml` (e.g. `gpt-4.1`, `gpt-5.4-nano`,
`claude-3.7-sonnet`, `Doubao-Seed-1.6-thinking`). To hit a different provider,
edit `default.base_url` + `headers` in `models.yaml` (or point
`VITA_MODEL_CONFIG_PATH` at another file, `config.py:13`) — **not** the CLI
flag. All three roles share one endpoint/config unless you reconfigure between
runs. `kwargs_adapter` (`llm_utils.py:174`) special-cases `claude*` (thinking
on/off), `deepseek*` (think), and `gpt-5*` (`max_completion_tokens`,
`reasoning_effort`).

**Resume pitfall — always pass an explicit `--save-to`.** If the `--save-to`
file already exists, `run_tasks` **prompts on stdin** ("Do you want to resume?
(y/n)") and **blocks** (`run.py:276-288`). In a headless loop this hangs. Rules:
- For a fresh batch, pass `--save-to <unique-name>` and ensure it does not
  pre-exist (the file lands at `data/simulations/<save-to>.json`, `run.py:132`).
- If `--save-to` is omitted, the file is auto-named
  `<timestamp>_<domain>_<agent>_<user>.json` (`make_run_name`, `run.py:92-105`)
  — unique by timestamp, so no resume prompt, but the name is not stable across
  runs.
- Resume *is* useful (it skips completed `(trial, task_id, seed)` tuples,
  `run.py:327-332`) but must be driven interactively; the loop itself should not
  rely on it.

Fast single-case rerun (Phase 2, Step 2.2) — one id, same models/seed, **fresh**
`--save-to`:

```bash
vita run --domain ota \
  --agent-llm <model> --user-llm <model> --evaluator-llm <model> \
  --task-ids <target_task_id> --num-trials 1 --seed 300 \
  --language chinese --save-to rerun_<id>
```

Re-analyze an existing results JSON **without rerunning** (re-grading after a
judge-model or `--evaluation-type` change, or reusing a prior loop's retest as
this loop's baseline):

```bash
# recompute reward_info from the embedded trajectories + embedded tasks:
vita run --re-evaluate-file data/simulations/<run>.json \
  --evaluation-type <trajectory|trajectory_full_traj_rubric|...> \
  --evaluator-llm <model> --save-to <out>
# re-run only specific tasks, then re-evaluate all together:
vita run --re-evaluate-file data/simulations/<run>.json --re-run \
  --task-ids <id ...> --agent-llm <model> --user-llm <model> \
  --evaluator-llm <model> --save-to <out>
```

`re_evaluate_simulation` (`run.py:703`) recomputes `reward_info` from the
embedded `messages` + the embedded `tasks`; unlike tau2 there is **no
`--fresh-tasks`** flag — the task definitions come from the file itself, so
task-definition drift is not a concern (and task-def edits are forbidden, 1.2).
The dominant variance source on re-grade is **judge noise** (a different
`--evaluator-llm`, or the same judge re-sampling its reasoning) — see 2.4.

Evidence-gathering helper (no rerun; for reading transcripts in Step 1.2 /
Step 2.3):

```bash
vita view --file data/simulations/<run>.json --only-show-failed   # browse failing transcripts
```

There is **no `tau2 review`-style LLM fault-isolation command** — no
`user_only_review` / `source=user` field is populated. Fault isolation is
manual: read `simulations[<i>].messages` + `reward_info.nl_rubrics[*].justification`
+ the task's `instructions` / `user_scenario.user_profile` / rubrics yourself.

`--max-concurrency` defaults to **1** (`config.py:83`); raise it only when the
endpoint tolerates it. `--csv-output <path>` appends a row-per-simulation CSV
(`run.py:168-174`). `--enable-think` toggles the agent's thinking/reasoning mode
(`config.py` `enable_think`, `llm_agent.py:54`; `kwargs_adapter`).

### 2.2 Result / Artifact Schema

`vita run` writes `data/simulations/<save-to>.json` (monolithic JSON; if
`--save-to` omitted → auto-named per 2.1; if it exists → resume prompt per
2.1). Top-level keys (verified against a real delivery run):

- `timestamp` — run timestamp.
- `info` — run config snapshot: `git_commit`, `num_trials`, `max_steps`,
  `max_errors`, `seed`, `agent_info` (`implementation`, `llm`, `llm_args`),
  `user_info` (`implementation`, `llm`, `llm_args`,
  `global_simulation_guidelines`), `environment_info` (`domain_name`,
  `tool_defs` — `null` by default).
- `tasks` — list of full `Task` objects (embedded so `--re-evaluate-file` can
  re-grade without the data dir). Each: `id` (string), `domain`, `environment`
  (the per-task DB: `time`, `user_id`, `orders`, `weather`, `location`,
  `hotels`/`attractions`/`flights`/`trains`, `user_historical_behaviors`),
  `user_scenario` (`user_profile` — the persona dict), `instructions` (the
  user's full task instruction), `evaluation_criteria` (`expected_states` with
  `required_orders` / `optional_orders` / `state_rubrics`, plus
  `overall_rubrics`), `message_history` (usually `[]`).
- `simulations` — list of `SimulationRun`, one per `(task, trial)` **that
  completed**. Tasks killed by infra after 3 retries are **dropped entirely**
  (no entry here — see 2.3.1), so `len(simulations)` can be < `len(tasks) *
  num_trials`.

Per-simulation entry (`simulations[*]`, verified keys): `id`, `task_id`,
`timestamp`, `start_time`, `end_time`, `duration`, `termination_reason`,
`agent_cost`, `user_cost`, **`reward_info`**, **`messages`** (the full
message-by-message transcript), `states` (`{"old_states": [...], "new_states":
[...]}` — orders/books/reservations split by `update_time` vs env time,
`orchestrator.py:374-394`), `trial`, `seed`.

`reward_info` (`RewardInfo`, `simulation.py:245-269`): `reward` (float; `1.0` =
pass, `0.0` = fail), `nl_rubrics` (list of `NLRubricCheck` — `nl_rubric` (the
rubric text), `met` (bool), `justification` (the judge's per-rubric reasoning,
with `[x]` round references)), `reward_breakdown` (`{NL_ASSERTION: <fraction of
rubrics met>}`), `info` (`note` for premature-termination/no-criteria cases;
`evaluation_method`, `num_windows`, `window_size` for sliding mode),
`window_evaluations` (per-window raw judge output, sliding modes only).

Field mapping for the loop:

- **pass/fail + reward:** `reward_info.reward` (float; `1.0` = pass, `0.0` =
  fail). Pass/fail ≡ `reward_info.reward == 1.0` (`is_successful`,
  `metrics/agent_metrics.py:13-17`).
- **full transcript / trajectory:** `messages` (`messages[*].tool_calls` on
  assistant messages; tool results are `role="tool"` `ToolMessage`s).
- **task id:** `task_id` (string); cross-ref `tasks[*].id` for the expected
  criteria.
- **expected outputs:** the matching `tasks[*].evaluation_criteria` —
  `expected_states[*].state_rubrics` + `overall_rubrics` (the NL rubrics the
  judge scores against), and `expected_states[*].required_orders` (the concrete
  `Order` objects the rubrics encode, useful as a structured cross-check when a
  rubric like "酒店订单里面有3晚的双床房" fails).
- **task intent / the WHY (mandatory Phase-1 evidence, per `SKILL.md` Step
  1.2):** VitaBench has **no `description.purpose` field** like tau2's — the
  designer's intent is distributed across three ground-truth fields, all
  subject-invariant (the tasks are identical across subject models):
  - `tasks[*].instructions` — the user's full natural-language task instruction.
    This is the cleanest **overall-intent + conditional-branch** source, e.g.
    *"下个月1号…预订3晚酒店…如果最高温度没超过30℃，就去月季大观园；如果超过了，就去张仲景博物馆…"*
    — the conditional (weather-gated attraction choice) is stated here, not
    recoverable from rubrics alone.
  - `tasks[*].user_scenario.user_profile` — the persona, which **scripts user
    behavior/pressure**: e.g. *"性格：做事急躁，经常催促，缺乏耐心等待过程"* (impatient,
    will rush you) signals a caves-to-pressure / don't-loop motive class;
    dietary/health constraints (*"忌高嘌呤"*) signal a must-respect-constraint
    class. This is the analog of tau2's `user_scenario.task_instructions`.
  - `tasks[*].evaluation_criteria.expected_states[*].state_rubrics` +
    `overall_rubrics` — the **atomic WHAT**: each rubric is a designer-stated
    success condition (*"酒店需要选择距离南阳市政府最近的全季酒店"*, *"酒店订单里面有3晚的双床房"*).
    These are the per-goal ground truth; a failing rubric's text is the
    cleanest statement of the specific atomic goal the agent missed.
  Read `instructions` + `user_profile` for the motive (the WHY), and the
  failing `state_rubric`/`overall_rubric` text for the atomic goal (the WHAT).
  Do not diagnose a failure's WHY without them.
- **tool calls made:** `messages[*].tool_calls` (assistant); results in
  `role="tool"` messages (`content` is JSON-stringified, `error` flag on
  exception, `environment.py:235-261`).
- **failure cause (the WHAT):** `termination_reason` (premature termination, see
  2.3) and `reward_info.nl_rubrics[*].met == False` + `.nl_rubric` + `.justification`
  (which rubric the judge marked unmet and the judge's reasoning). This is an
  **LLM-judged WHAT** — softer than tau2's mechanical `db_match`/`action_match`;
  the `justification` is the judge's opinion and can be wrong (2.3.1).

A real failing case looks like: `termination_reason = "agent_stop"` (normal
end), `reward = 0.0`, and one or more `nl_rubrics[*].met = false` with a
`justification` citing the rounds where the assistant fell short (e.g. "the
assistant never called `create_ota_order`; the user said they would book
themselves, which per the rubric does not satisfy"). A premature-termination
case looks like: `termination_reason = "max_steps"` (or `too_many_errors` /
`invalid_agent_message`), `reward = 0.0`, `reward_info.info.note =
"Simulation terminated prematurely. Termination reason: max_steps"`, and
`nl_rubrics = null` (the judge was never run).

### 2.3 Failure Taxonomy

No LLM is required to **derive the key** — cluster keys derive mechanically
from `reward_info` fields already present in the results JSON (a small helper
may compute them, but none ships). The **WHY** still requires reading the
transcript + task intent (2.2) — the key is a pointer, not a root cause. For
each `SimulationRun` `s` with `task = tasks[task_id]`:

1. **Missing-sim / infra bucket** (check first). If `s` is **absent** from
   `simulations` (i.e. `len(simulations) < len(tasks) * num_trials` for the
   domain/trial), the task was killed by an exception in `run_task` after 3
   retries (`run.py:492-520`) and **left no entry** — VitaBench has no
   `infrastructure_error` termination reason. Key = `INFRA_KILLED:<task_id>`.
   Confirm by rerunning `--task-ids <id> --num-trials 1` with a fresh
   `--save-to`; if it completes, it was transient infra (2.3.1).
2. **Premature-termination bucket** (next priority). If `s.termination_reason`
   is present and `not in {"user_stop", "agent_stop"}` → key =
   `TERM:<termination_reason>`. The enum (`simulation.py:314-319`) is
   `max_steps`, `too_many_errors`, `invalid_agent_message`. All force
   `reward = 0.0` (`evaluator.py:18-28`) and **skip the judge** (`nl_rubrics =
   null`). Fold in `reward_info.info.note` starting with
   `"Simulation terminated prematurely"`.
3. **Rubric-failure buckets** (when `termination_reason` is `user_stop` /
   `agent_stop` but `reward < 1`). Iterate `reward_info.nl_rubrics`; for each
   with `met == False`, classify the rubric into a **category** by the keyword
   rule below and emit one sub-key `RUBRIC_FAIL:<category>`. The cluster key =
   the sorted, `|`-joined tuple of failing categories, e.g.
   `RUBRIC_FAIL:ORDER_DETAIL_QTY|ORDER_PLACED`; a single category → that one
   sub-key. Keep the per-case `rubric_<i>` index, the `nl_rubric` text, and the
   judge's `justification` in the cluster body for the WHY read — the category
   key is cross-task comparable; the index is not.
4. **`PASS`** when `reward == 1.0` (not a failure; excluded from clustering).

**Rubric category derivation (keyword rule, mechanical).** Match the rubric
text case-insensitively; zh keywords first (default language is Chinese), en in
parens. First matching category wins, in the order listed; `RUBRIC_OTHER` is
the residual.

| Category | Match when the rubric text contains | Meaning / what the agent had to do |
|---|---|---|
| `REFUSE_INELIGIBLE` | 拒绝 / 不允许 / 不可取消 / 不应 / 不能取消 / 不符合 / refuse / not allowed / must not / cancel not permitted | Agent should **refuse / not act** (cancel/refund/modification is ineligible) |
| `INTENT_CONDITIONAL` | 如果 / 若 / 当…时 / 根据…决定 / if / when / depending on / based on | Agent had to **branch** on a condition (weather/temp/time/availability) before acting |
| `ORDER_PLACED` | 下单 / 生成订单 / 创建订单 / 完成下单 / 实际下单 / place order / create order / order must be placed | An order **must actually be created** (tool call succeeded) |
| `ORDER_DETAIL_QTY` | 数量 / 份 / 几份 / ×N / X份 / quantity / how many | Order **quantity** must be exact (no user-compromise) |
| `ORDER_DETAIL_TIME` | 送达时间 / 时间 / 日期 / 不晚于 / 送达日期 / 预订时间 / date / delivery time / not later than | Order **time/date** must be exact / not later than expected |
| `ORDER_DETAIL_STORE` | 选哪家 / 距离…最近 / 品牌 / 门店 / 选择… / 最近 / nearest / closest / brand / which store | Correct **store/hotel/brand/selection** (often requires a lookup) |
| `ORDER_DETAIL_PRODUCT` | 房型 / 商品 / 口味 / 菜品 / 票种 / room type / product / flavor / ticket type | Correct **product/room/ticket type** within the order |
| `SEARCH_QUERY` | 查询 / 搜索 / 查询可得 / 查 / search / query / look up / retrieve | Agent had to **perform a lookup** to obtain a fact needed downstream |
| `COMMUNICATE` | 告知 / 回复 / 向用户说明 / 推荐 / 提醒 / confirm to user / inform / tell the user / convey / recommend | A fact had to be **conveyed to the user** (query-tool results alone don't count) |
| `CONV_END` | 结束对话 / ###STOP### / stop / end the conversation | Agent had to **end the conversation** with `###STOP###` once done |
| `RUBRIC_OTHER` | (none of the above matched) | Residual — **sub-cluster before fixing or deferring** (procedure below) |

| Key prefix | Meaning | How it's derived | Actionable by default? |
|---|---|---|---|
| `INFRA_KILLED:<task_id>` | Task absent from `simulations` — exception after 3 retries | `len(simulations)` gap vs `len(tasks)*num_trials`; `run_task` retry log | **No** (infra, 2.3.1) — confirm by clean rerun |
| `TERM:<reason>` | Episode ended non-normally; reward forced 0, judge skipped | `termination_reason` enum; `reward_info.info.note` | `max_steps` → maybe (agent looped/stalled — read transcript); `too_many_errors` → usually yes (repeated failing tool calls → docstring/args); `invalid_agent_message` → maybe (3× empty messages → behavioral/capability) |
| `RUBRIC_FAIL:<cat>` | One or more NL rubrics the judge marked unmet | `nl_rubrics[*].met == False` + keyword category rule above | **Yes, typically the priority** — but the judge is an LLM; verify the rubric + transcript before fixing (2.3.1 judge noise) |

**Sub-clustering a residual `RUBRIC_OTHER` bucket (mandatory before fixing *or*
deferring it).** `RUBRIC_OTHER` is a catch-all by derivation; its size is exactly
why it's a priority candidate. For each `RUBRIC_OTHER` case, read the failing
`nl_rubric` text + the judge `justification` and classify by **what the rubric
demands** (the WHAT) and — from `instructions` / `user_profile` — the **motive**
(the WHY). Common sub-classes observed in this benchmark's rubric style:

- **multi-step procedural** — the rubric requires a sequence (lookup → select →
  place order → confirm); a failure here is usually a **missed step**, fixable
  via a domain-agnostic procedural rule in the agent instruction ("after
  placing an order, restate the order summary and total to the user before
  ending").
- **constraint-respect** — a persona constraint (dietary, health, budget) the
  agent violated; fixable via an agent-instruction rule ("re-check the user
  profile's constraints before finalizing an order").
- **caves-to-pressure-on-ineligible** — the persona is impatient/pushy
  (`user_profile` 性格) and the agent performed an action it should have refused
  or shortcut a verification step to appease the user. This is the high-value
  fixable class; do **not** misframe it as a capability gap — the fix is a
  "verify-eligibility / don't shortcut under pressure" rule in the agent
  instruction (the only prompt surface available, 1.2). Mirror of tau2's
  under-pressure class, but it lives in the agent instruction here, not a
  domain policy.
- **judge-misread** — the rubric was actually met but the judge marked it unmet
  (the `justification` mis-cites the transcript, or applies strictness the
  template doesn't warrant). This is **non-actionable** (2.3.1 judge noise),
  not an agent bug — confirm by re-grading before logging it.

Without this sub-cluster, a `RUBRIC_OTHER` fix is a guess: procedural,
constraint, under-pressure, and judge-misread need different surfaces (or no
surface at all). Lumping them hides a fixable majority behind a judge-noise
minority (or vice versa).

**Same discipline for the named categories:** a `RUBRIC_FAIL:ORDER_PLACED`
bucket is a WHAT, not a WHY — the same "order not placed" can come from (a) the
agent never called `create_*_order` (procedural gap → agent instruction), (b)
it called the tool but the call errored and the agent didn't notice
(`too_many_errors`-adjacent → tool docstring / a "check the tool result before
claiming success" instruction), (c) the user said "I'll book it myself" and the
agent deferred (judge rule says this is unmet — possibly borderline, 2.3.1), or
(d) the judge missed the successful call (judge noise, 2.3.1). Read the
transcript for 1-2 representative cases before proposing *or deferring*.

**This taxonomy is a hypothesis, not settled fact, until checked against real
evidence.** The mechanical key is a pointer to a bucket of failures, not proof
they share one root cause. Always read `simulations[<i>].messages` + the task
intent (2.2) for 1-2 representative cases before proposing a fix *or deferring
a cluster* from a key alone — a `RUBRIC_FAIL:COMMUNICATE` and a
`RUBRIC_FAIL:ORDER_PLACED` can both trace back to the user simulator never
revealing a needed ID (simulator drift, 2.3.1), not an agent gap; and
conversely a `RUBRIC_OTHER` bucket can be a fixable procedural majority hiding
behind an uninformative label.

#### 2.3.1 Determining Actionability

A cluster is non-actionable — safe to defer or drop — only when it resolves to
one of these, **confirmed by evidence** (a transcript read, a clean rerun, a
re-grade), not merely because it looks like one. VitaBench's dominant
non-agent cause is **judge noise** (reward is LLM-judged), which tau2 does not
have — weight it accordingly.

- **Transient API / infra failure** — a task **missing** from `simulations`
  (`INFRA_KILLED:<task_id>`, an exception after 3 retries in `run_task`,
  `run.py:492-520`; no `infrastructure_error` termination reason exists).
  Confirm by rerunning clean (`--task-ids <id> --num-trials 1`, fresh
  `--save-to`); if it completes, it was infra.
- **Judge noise / evaluator variance** — the SAME trajectory scores differently
  under a different `--evaluator-llm` (or even re-graded with the same judge,
  since the judge call in `evaluator_traj.py:223` does **not** receive a seed
  and `DEFAULT_LLM_EVALUATOR = claude-3.7-sonnet` runs with thinking enabled,
  `models.yaml`). This is the **primary** non-agent cause here. Confirm by
  re-grading the failing sims with `vita run --re-evaluate-file <run>.json
  --evaluator-llm <same-or-stronger> --save-to <out>`; a failure that flips to
  pass on re-grade is judge noise, not an agent bug. Surface the re-grade
  agreement rate in the design report before crediting any fix.
- **User-simulator drift** — the LLM user failed to follow
  `user_scenario.user_profile` / `instructions` (didn't reveal a needed ID,
  volunteered the wrong value, stopped early via `STOP`/`TRANSFER`/`OUT_OF_SCOPE`).
  No `tau2 review --mode user` ships — verify by reading the transcript: did the
  user ever state the information the failing rubric needs? If not, the
  simulator withheld it and the agent could not have known; that is drift, not
  an agent gap. (Editing the simulator to "fix" this changes the benchmark —
  out of scope, 1.2.)
- **Borderline / ambiguous grading** — the eval templates carry explicit
  strictness rules that make some defensible agent answers fail: query-tool
  returns don't count (only the user-facing reply does), user compromise
  ("fewer items is okay", "later delivery is fine") does **not** satisfy strict
  quantity/time rubrics, and addresses use functional-equivalence. A failure
  where the agent's answer is defensible but the judge applied these narrowly
  is a possible over-strict grading case — flag it, do not adopt a fix that
  only chases the specific rubric's strict reading.
- **Evaluator gap** — `evaluation_criteria is None` or
  `not expected_states and not overall_rubrics` makes the evaluator return
  `1.0` with `info.note = "No rubric to evaluate"` (`evaluator_traj.py:42-57`).
  Such a task silently passes regardless of behavior; surface as a smell, not a
  fix target.

There is **no fully automatic fault-assignment classifier** that partitions a
0-reward into agent-vs-infra-vs-judge-vs-simulator; the mechanical key above is
the deterministic starting point and the transcript is the tie-breaker. Always
cite the specific evidence checked (which transcript, which rubric
`justification`, which re-grade result, which divergence) in
`OPTIMIZATION-LOG.md` when tagging a cluster non-actionable — a "non-actionable"
tag with no cited evidence must be re-verified before it can satisfy the
generic loop's stop condition 2.

### 2.4 Confidence Policy

VitaBench is **stochastic in structure, and the evaluator makes it less
deterministic than tau2 even at default config.** Three roles are sampled LLMs
— agent, user simulator, and **evaluator/judge** — and all three flow through
`generate()`. Agent and user default to `temperature: 0.0` and receive the
forwarded seed (`set_seed` → `llm_args["seed"]`, `llm_agent.py:114-121`), so a
`(task, trial, seed)` tuple is reproducible for those two. The **judge does
not** receive a seed (`evaluator_traj.py:223-227` calls `generate(model=llm_evaluator,
messages=..., **llm_args_evaluator)` with no seed), and
`DEFAULT_LLM_EVALUATOR = "anthropic.claude-3.7-sonnet"` (`config.py:95`) runs
with `thinking: enabled` (`models.yaml`) — so re-grading the same trajectory
can flip the reward. This judge variance is the dominant noise source and the
main reason confidence discipline matters more here than in tau2.

- **`--num-trials N`** (`cli.py:33-38`, default `DEFAULT_NUM_TRIALS = 1`,
  `config.py:84`) runs each task N independent times (`run.py:400-408`); each
  sim records `trial` and `seed`. `pass^k` (`pass_hat_k`,
  `agent_metrics.py:52-65`): `pass^1` = mean over tasks of (successes / N);
  `pass^k` = fraction of tasks where all k-of-N trial combinations succeed.
  `pass@k` and `average@k` are also computed (`agent_metrics.py:68-102`).
- **`--seed`** (default 300) seeds Python's RNG, which emits **one seed per
  trial** shared across all tasks in that trial (`run.py:242-244`); it does
  **not** give per-task seeds and does **not** reorder tasks. It does **not**
  seed the evaluator.
- **`--evaluator-llm`** (`cli.py:109-114`, default `DEFAULT_LLM_EVALUATOR`) is
  the reward signal. **Treat it as fixed across the whole optimization** —
  swapping it mid-run changes winnability and makes every prior number
  non-comparable. If you must compare two judges, re-grade the *same* run with
  both via `--re-evaluate-file` and report the agreement rate; do not mix
  judge models across loops in `OPTIMIZATION-LOG.md`'s trajectory table without
  labeling them.

- **Fast optimization loop (default):** `--num-trials 1 --seed 300`,
  `temperature: 0.0`, one fixed `--evaluator-llm`. A single-trial pass/fail is
  sufficient evidence during the loop; don't pay for 3× trials on every
  iteration. (Caveat: a single trial is a point estimate, not
  confidence-grade — see credit threshold.)
- **Confidence mode (opt-in):** `--num-trials 3` when a result looks flaky, a
  failure is high-stakes, or before crediting a fix (the minimum to compute
  `pass^2`/`pass^3`); `--num-trials 5` for finer resolution. Cost scales
  linearly (each trial = full re-run of the sample).
- **Credit metric & threshold:** report **`pass^1`** (mean per-task success
  across trials, `compute_metrics` → `avg_reward` / `pass_hat_ks`, printed by
  `vita run` and exportable via `--csv-output`) as the primary credit metric.
  Pragmatic acceptance bar: **`pass^1 ≥ 0.80` on the held-out task-id set with
  `--num-trials ≥ 3`, identical `--seed`, and the same `--evaluator-llm`**,
  re-graded once with `--re-evaluate-file` (same judge) before reporting, so
  the credited number is not a single lucky judge draw. Single-trial `reward`
  is a point estimate, not a confidence-grade score.
- **Determinism sanity check:** for temp-0 agent+user, trial variance should be
  ~0. If `pass^1` differs across trials at temp 0, suspect **judge variance**
  (re-grade to confirm) or **infra kills** (`len(simulations)` gap, 2.3) —
  surface those via the `INFRA_KILLED`/re-grade path rather than re-running
  blindly. A before/after retest that flips a case pass→fail→pass across
  re-grades is judge noise, not a regression caused by your edit.

Used in Step 1.2 (how much to trust one failing transcript + judge
justification before clustering) and Step 2.4 (how much to trust a retest
before crediting a fix) of `SKILL.md`.

## Run Config (Optional)

`SKILL.md`'s Step 0 lets a run config file supply 1.3/2.1/2.4's inputs plus the
generic loop's stop conditions, instead of restating them in chat every
invocation. Configs live in `configs/<name>.yaml` in this same skill folder —
one file per (domain, sampling, model) combination you run repeatedly, e.g.
`configs/vita-ota.yaml`.

**Three LLM roles, not one "model" — and the judge is a role, not just a
grader.** Conflating the agent under test with the user simulator or the
evaluator is how a run stops being comparable to anything measured before —
see `subject_model` / `simulator_model` / `judge_model` below. Only
`subject_model` is under optimization; `simulator_model` and `judge_model`
should be treated as fixed (`hold_fixed: true`) unless the user explicitly asks
to change the benchmark itself. **Swapping the judge mid-optimization is the
easy mistake here** — because VitaBench's reward is the judge's output
(2.1/2.4), changing `--evaluator-llm` silently rescores every prior result.

The config holds only five sections — **roles, sampling, confidence,
concurrency, stopping**. It is not the place for accumulated results: any fact
discovered by running a loop (which subject models have already consumed the
held-out task-id list, what's true only for one specific subject, a protocol for
swapping subjects or judges) belongs in `OPTIMIZATION-LOG.md` instead (see
`SKILL.md`'s Artifact Conventions). Keeping findings there and only sampling
*policy* here is what lets the same yaml serve any subject model without
silently carrying over another model's results.

Schema (every field optional — anything omitted falls back to this adapter's
stated default, e.g. `--num-trials 1` for the fast loop):

```yaml
domain: ota                           # --domain (single: delivery|instore|ota;
                                      #   cross: delivery,instore,ota — default cross)
language: chinese                     # --language (chinese|english; selects task file + prompts)

# --- Roles -------------------------------------------------------
subject_model:                        # the agent under test: --agent-llm
  id: gpt-4.1                         # a `name` from models.yaml (NOT a litellm prefix)
  config: configs/models.yaml         # or set via VITA_MODEL_CONFIG_PATH; routing is by
                                      #   models.yaml default.base_url, one OpenAI-compatible endpoint
  baselined: true|false               # false = no measurements exist yet for this subject
  selection_evidence: <one-line — why this model, what alternates it beat>

simulator_model:                       # the measuring instrument (user): --user-llm
  id: gpt-4.1                         # a models.yaml name
  hold_fixed: true                     # swapping this changes the benchmark, not the subject
  note: >                              # why it's fixed
    the user simulator's paraphrases decide whether required info is
    revealed and whether conveyed-fact rubrics are satisfied, so
    changing it changes winnability

judge_model:                           # the reward signal: --evaluator-llm
  id: anthropic.claude-3.7-sonnet      # a models.yaml name; DEFAULT_LLM_EVALUATOR
  hold_fixed: true                     # swapping this rescores every prior result (2.4)
  note: >                              # why it's fixed
    VitaBench reward IS the judge's rubric verdict (no DB hash), and the
    judge is unseded + thinking-enabled, so a judge swap changes every
    number; re-grade with --re-evaluate-file if you must compare judges

optimizer: claude-code                 # reads results, writes fixes — not CLI-configurable;
                                      # it's the session running this skill

# --- Sampling ------------------------------------------------------
tuning_sample:                         # 1.3 optimization sample — never used for regression checks
  task_ids: ["D0812006", "D0812007"]   # explicit --task-ids (opaque strings), OR:
  # num_tasks: 30                      # --num-tasks (first N of the domain, no shuffle)
  purpose: <one-line — diagnosis sample; pass rate here is a training metric>

mechanism_holdout:                     # 1.3 held-out sample — DISJOINT task-id list (no split files;
                                      #   disjointness is manual). Policy only — the concrete registry
                                      #   (which mechanisms enumerated, their task-id lists, which
                                      #   subject models already consumed one) lives in OPTIMIZATION-LOG.md.
  task_ids: ["D0812050", "D0812051"]   # must not overlap tuning_sample.task_ids
  min_tasks: 15
  purpose: <one-line>

# --- Confidence ------------------------------------------------------
confidence:                            # 2.4 — omit entirely to use the adapter's stated defaults
  screen_num_trials: 1
  confirm_num_trials: 3
  credit_metric: pass_1                # VitaBench's pass^1 / avg_reward; pair with a re-grade agreement check
  regrade_before_credit: true          # re-run --re-evaluate-file (same judge) before crediting, to rule out judge noise
  paired_arms: true                    # pair by TASK id, not episode — editing the agent
                                      #   regenerates the episode, so episodes can't be held fixed

# --- Concurrency ------------------------------------------------------
concurrency:                           # --max-concurrency (default 1)
  active: <n>

# --- Stopping ------------------------------------------------------
stop_conditions:                       # generic-loop session inputs, not adapter facts —
  loop_budget: 5                       # cached here anyway since they're set once per session
  target: <e.g. mechanism_generalisation, or target_pass_1: 0.90>
  credit_rule: <the precise rule that credits a fix, if not a plain pass^1 threshold;
    e.g. "pass^1 held-out ≥ 0.80 AND re-grade agreement ≥ 0.9 on the flipped cases">
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
  task whose rubric is unsatisfiable by construction, an evaluator-gap smell
  (2.3.1, `No rubric to evaluate`), or a rubric the judge systematically
  mis-grades across subjects.
- **Subject-specific findings** — tied to one `subject_model.id`; tag with
  `measured_on: <model>` and treat as unverified for any other model until
  re-confirmed.
- **Judge-specific findings** — tied to one `judge_model.id`; tag with
  `judged_by: <model>`. A cluster that is judge-noise under one judge may be a
  real agent bug under another; record the re-grade agreement rate.
- **Mechanism-holdout registry** — one entry per mechanism a credited fix
  targets: the trigger, the population, the disjoint held-out `task_ids` (no
  split files — the list is yours to maintain), and which subject models have
  already consumed it as a holdout (an entry already used for one subject is
  still valid unseen for a different one).
- **On a subject-model swap** — `git checkout HEAD` the editable surface (1.2),
  re-baseline the tuning sample, open a new `OPTIMIZATION-LOG.md` section
  rather than appending, treat prior subject-specific findings as unverified,
  and either run the new subject in a fresh session with no access to the
  log/loop reports or pre-register the target mechanism before comparing to
  prior findings — the optimizer is a Claude Code session, and one that has
  read a previous model's logs is primed to re-find that model's mechanisms.
- **On a judge-model swap** — re-grade the baseline run with the new judge via
  `--re-evaluate-file` to get a new baseline `pass^1` under the new reward
  signal; do not carry over old `pass^1` numbers. Treat prior judge-specific
  findings as unverified under the new judge.
