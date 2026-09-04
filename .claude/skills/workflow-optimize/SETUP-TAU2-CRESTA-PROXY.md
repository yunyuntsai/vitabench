# Setup: This Execution Environment (tau2-bench)

Environment/auth notes for running `tau2 run` from this workspace. This is
**not** part of the tau2 adapter (`ADAPTER-TAU2BENCH.md`) — it's specific to
the current deployment environment, not to tau2 as a benchmark. A different
environment with real model API keys can use `ADAPTER-TAU2BENCH.md` unchanged
and skip this file entirely.

## Base setup

```bash
# repo uses uv (pyproject + uv.lock); the venv is .venv/
uv sync                # or: uv run tau2 --help   (auto-creates the venv)
```

Run all `tau2` commands through `uv run tau2 ...` so the project venv (and its
litellm) is used. A bare `python` may hit a pyenv shim and not be the project
interpreter.

## Models reachable from this environment

Two paths are wired up here, routed per-model by litellm's prefix convention
(`llm_utils.py:417-423` → `litellm.completion(model=...)`):

| path | model prefix | env vars | backend | verified model ids |
|---|---|---|---|---|
| Local shim → Cresta Vertex proxy | `anthropic/` | `ANTHROPIC_API_BASE`, `ANTHROPIC_API_KEY` | Google Vertex (Claude) **and** Fireworks (GLM), per model | `anthropic/claude-sonnet-4-5`, `anthropic/glm-5.2` |
| Direct to Fireworks | `fireworks_ai/` | `FIREWORKS_API_KEY` | Fireworks | `fireworks_ai/accounts/fireworks/models/deepseek-v4-flash`, `.../deepseek-v4-pro`, `.../deepseek-v4-flash-0731` |

### What the Cresta Vertex proxy actually serves

The proxy at `https://dev-proxy.ops2.internal.cresta.ai/proxy/vertex` exposes
a Vertex-*shaped* API (every request hits
`/v1/projects/cresta-ml/locations/us-east5/publishers/anthropic/models/<model>:rawPredict`)
but routes per model to different backends — the `publishers/anthropic` in the
path is a routing convention, not a statement about the backend. Probe results
(this session):

- **Claude** (`claude-*`) → served, backend = Google Vertex (response `id`
  prefix `msg_vrtx_`). Verified: `claude-sonnet-4-5` (resolves to
  `claude-sonnet-4-5-20250929`).
- **GLM** (`glm-*`) → served, backend = Fireworks (response `model` =
  `accounts/fireworks/models/glm-5p2`). Verified: `glm-5.2`.
- **DeepSeek / Kimi / MiniMax** → **not served** by this proxy under any alias
  tried (`Unsupported model`). Use Fireworks direct (below) for DeepSeek V4.

There is no model-list endpoint on the proxy (all GETs return `405`); to
discover whether an id is served, POST a minimal `rawPredict` and check the
code (`200` served, `404 Unsupported model` not).

### Why the shim exists

`scripts/anthropic_to_vertex_proxy.py` is a local HTTP shim that translates an
Anthropic Messages-API request into the Cresta proxy's Vertex `rawPredict`
URL, attaching a static `VERTEX_PROXY_TOKEN` bearer and (for `claude-*`/`glm-*`
models) the `anthropic_version` + cache-control the proxy expects. The `claude`
CLI's Vertex mode can't use a static `uuid:secret` token (it needs Google ADC),
and the proxy's native `/proxy/anthropic` rejects that token — so the shim is
the working path for token auth. litellm's `anthropic/` prefix speaks the same
Anthropic Messages format the shim consumes, so `--agent-llm anthropic/glm-5.2`
flows litellm → shim → Cresta proxy → backend unchanged.

## Running through the Cresta proxy (Confirmed working 2026-08-02)

1. Launch the shim in a separate terminal (it stays up; the loop talks to it):

   ```bash
   VERTEX_PROXY_TOKEN=<uuid:secret> \
   python scripts/anthropic_to_vertex_proxy.py --port 4113
   # falls back to CRESTA_VERTEX_TOKEN if VERTEX_PROXY_TOKEN is unset
   ```

   `--port` defaults to 4112; pick any free port and point `ANTHROPIC_API_BASE`
   at it below. The shim prints `anthropic->vertex shim on http://127.0.0.1:<port> ...`
   and the last 6 chars of the token on startup.

2. Export the env vars litellm reads. `ANTHROPIC_API_KEY` is a dummy — the shim
   ignores it and uses its own `VERTEX_PROXY_TOKEN`:

   ```bash
   export ANTHROPIC_API_BASE=http://127.0.0.1:4113   # the shim, not the proxy
   export ANTHROPIC_API_KEY=dummy
   ```

3. Smoke-test the shim before a batch (one call, ~seconds):

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:4113/v1/messages \
     -H 'Content-Type: application/json' -H 'x-api-key: dummy' \
     -d '{"model":"glm-5.2","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}'
   # expect 200
   ```

4. Run tau2 with `anthropic/<model>` model strings:

   ```bash
   uv run tau2 run --domain airline \
     --agent-llm anthropic/glm-5.2 --user-llm anthropic/glm-5.2 \
     --task-ids 7 --num-trials 1 --seed 300 --save-to smoke
   ```

## Running DeepSeek V4 direct via Fireworks (Confirmed working 2026-08-02)

DeepSeek V4 is on Fireworks but **not** on the Cresta proxy, so call Fireworks
directly for that model (the agent and user sim can use different paths in the
same run — litellm routes per model string):

```bash
export FIREWORKS_API_KEY=<fw_...>
uv run tau2 run --domain airline \
  --agent-llm fireworks_ai/accounts/fireworks/models/deepseek-v4-flash \
  --user-llm anthropic/glm-5.2 \
  --task-ids 7 --num-trials 1 --seed 300 --save-to smoke_dsv4
```

The exact Fireworks ids (from `GET https://api.fireworks.ai/inference/v1/models`):
`accounts/fireworks/models/deepseek-v4-flash`, `.../deepseek-v4-pro`,
`.../deepseek-v4-flash-0731`. Use `-pro` for higher quality / higher cost.

## Known noise fix already applied to this repo

`src/tau2/utils/llm_utils.py` sets `litellm.suppress_debug_info = True` and
downgrades the cost-calc `BadRequestError` to `debug`. Without it, every agent
step whose response carries a proxy-resolved model name with no provider prefix
(e.g. `accounts/fireworks/models/glm-5p2`) spams `Provider List: ...` to stdout
and logs an `ERROR` — caught and harmless (cost returns `0.0`), but loud enough
to look like a failure. If that noise reappears, confirm those two edits are
still present before debugging anything else.

## Verify with one task before trusting a batch

Always run a single-task smoke (`--task-ids <id> --num-trials 1`) and confirm
`data/simulations/<save-to>/results.json` has a `simulations[*].reward_info`
before launching a full split — a batch you can't analyze is worse than not
running one.
