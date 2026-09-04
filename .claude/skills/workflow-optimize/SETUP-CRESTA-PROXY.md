# Setup: This Execution Environment

Environment/auth notes for running `eval_optimize.py` from this workspace.
This is **not** part of the tau-bench adapter (`ADAPTER-TAUBENCH.md`) — it's
specific to the current deployment environment, not to tau-bench as a
benchmark. A different environment with real model API keys can use
`ADAPTER-TAUBENCH.md` unchanged and skip this file entirely.

## Base Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires a real, directly-callable model API key for whichever
`--model-provider`/`--user-model-provider` you use (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, etc., per litellm's convention). Verify with one task
before trusting a full batch.

## Running Through Cresta's Dev Proxy (Confirmed Working 2026-07-24)

In this environment, `ANTHROPIC_API_KEY` is a Claude-Code-scoped
`<uuid>:<secret>` token, not a real `sk-ant-...` key. Cresta's dev-proxy
(`dev-proxy.ops2.internal.cresta.ai`) fronts two different backends and only
one of them accepts that token:

- `/proxy/anthropic` (`ANTHROPIC_BASE_URL`) is a **bare passthrough** — it
  forwards whatever `x-api-key` you send straight to real Anthropic, so it
  needs a genuine Anthropic key. The Claude-Code-scoped token 401s here no
  matter what headers are added (`"x-api-key header is required"` →, with
  the `X-DEV-PROXY-TRANSIENT-API-KEY`/`X-DEV-PROXY-LOGGING-MODE` headers
  added, → `"invalid x-api-key"`). **Dead end for this key.**
- `/proxy/bedrock` (`AWS_BEDROCK_RUNTIME_ENDPOINT`) **validates that same
  `ANTHROPIC_API_KEY` value itself**, sent as a plain `x-api-key` header,
  and once accepted forwards to real AWS Bedrock using its own service
  credentials — the client's `AWS_PROFILE` just needs to produce *some*
  valid SigV4 signature (e.g. `dev-servers_dev`, an SSO profile — run
  `aws sso login --profile dev-servers_dev` if the session has expired);
  the proxy authorizes model access itself, not the IAM role. **This is the
  working path.**

`eval_optimize.py`'s `configure_cresta_dev_proxy()` wires this up
automatically: it sends `ANTHROPIC_API_KEY` as `x-api-key` on every
`completion()` call (patching `litellm.headers` *and* each tau_bench
module's already-imported `completion` reference directly, since litellm's
bedrock dispatch — unlike azure/anthropic — doesn't fall back to the
`litellm.headers` global) and defaults `AWS_BEDROCK_RUNTIME_ENDPOINT` to the
proxy. You still need to supply `--model-provider bedrock` with **Bedrock
cross-region model IDs** (older bare `anthropic.claude-3-*` IDs 404 on this
proxy — only `us.anthropic.claude-*` cross-region IDs work), and a working
`AWS_PROFILE`:

```bash
AWS_PROFILE=dev-servers_dev AWS_REGION_NAME=us-west-2 \
python3 eval_optimize.py --env retail \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0 --model-provider bedrock \
  --user-model us.anthropic.claude-sonnet-4-5-20250929-v1:0 --user-model-provider bedrock \
  --task-ids 0 --num-trials 1 --max-concurrency 1
```
