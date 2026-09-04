#!/usr/bin/env python3
"""Local shim: Anthropic Messages API  ->  Cresta Vertex proxy (:rawPredict /
:streamRawPredict) with a STATIC bearer token.

Why this exists:
    The `claude` CLI's Vertex mode (CLAUDE_CODE_USE_VERTEX=1) authenticates ONLY
    via Google ADC -- it cannot use a static `uuid:secret` bearer token. And the
    Cresta `/proxy/anthropic` (native) endpoint rejects the Vertex `uuid:secret`
    key (wants a real Antxahropic x-api-key). So to let cc_user_con drive Claude
    through the Vertex key, we run this tiny shim and point the CLI's
    ANTHROPIC_BASE_URL at it. The Vertex response body is already standard
    Anthropic Messages format, so the translation is: pick the model + verb from
    the request, move `model` out of the body, add `anthropic_version`, attach the
    Bearer token, and stream the bytes straight back.

Run:
    VERTEX_PROXY_TOKEN=<uuid:secret> python anthropic_to_vertex_proxy.py --port 4112

Point the claude CLI (or cc_user_con's anthropic_base_url branch) at it:
    ANTHROPIC_BASE_URL=http://127.0.0.1:4112   ANTHROPIC_API_KEY=dummy
The request body's "model" (e.g. claude-sonnet-4-5) selects the Vertex model.

Env:
    VERTEX_PROXY_TOKEN     <uuid:secret>  (falls back to CRESTA_VERTEX_TOKEN)
    CRESTA_VERTEX_PROXY_BASE  default https://dev-proxy.ops2.internal.cresta.ai/proxy/vertex
    VERTEXAI_PROJECT       default cresta-ml
    VERTEXAI_LOCATION      default us-east5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, TCPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOKEN = os.environ.get("VERTEX_PROXY_TOKEN") or os.environ.get("CRESTA_VERTEX_TOKEN", "")
BASE = os.environ.get(
    "CRESTA_VERTEX_PROXY_BASE",
    "https://dev-proxy.ops2.internal.cresta.ai/proxy/vertex",
).rstrip("/")
PROJECT = os.environ.get("VERTEXAI_PROJECT", "cresta-ml")
LOCATION = os.environ.get("VERTEXAI_LOCATION", "us-east5")
ANTHROPIC_VERTEX_VERSION = "vertex-2023-10-16"

# Top-level fields the Vertex Messages endpoint (anthropic_version
# vertex-2023-10-16) accepts. The `claude` CLI sends extras (context_management,
# etc.) that Vertex rejects with HTTP 400 "Extra inputs are not permitted", so
# the shim strips anything not in this set before forwarding. `model` is moved
# into the URL; `anthropic_version` is added by the shim.
_VERTEX_ALLOWED_FIELDS = {
    "messages",
    "system",
    "max_tokens",
    "metadata",
    "stop_sequences",
    "stream",
    "temperature",
    "top_k",
    "top_p",
    "tools",
    "tool_choice",
    "thinking",
    "anthropic_version",
}


def vertex_url(model: str, stream: bool) -> str:
    verb = "streamRawPredict" if stream else "rawPredict"
    return (f"{BASE}/v1/projects/{PROJECT}/locations/{LOCATION}"
            f"/publishers/anthropic/models/{model}:{verb}")


def _inject_cache_control(payload: dict) -> None:
    """Add cache_control to the last system block and last tool so Vertex caches
    the shared prefix (persona + skills + tool definitions). The claude CLI
    doesn't send cache_control in ANTHROPIC_BASE_URL mode, so we inject it here."""
    # system: mark the last block
    system = payload.get("system")
    if system and isinstance(system, list) and system:
        system[-1]["cache_control"] = {"type": "ephemeral"}
    # tools: mark the last tool definition
    tools = payload.get("tools")
    if tools and isinstance(tools, list) and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # close at end-of-response -> clean SSE EOF

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._json(code, {"type": "error", "error": {"type": "shim_error", "message": msg}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)

        # count_tokens: Vertex passthrough has no cheap equivalent; return a rough
        # char/4 estimate (the CLI only uses this for pre-flight sizing).
        if self.path.rstrip("/").endswith("count_tokens"):
            return self._json(200, {"input_tokens": max(1, len(raw) // 4)})

        try:
            payload = json.loads(raw)
        except Exception as e:
            return self._err(400, f"bad json: {e}")
        model = payload.pop("model", None)
        if not model:
            return self._err(400, "missing 'model' in body")
        stream = bool(payload.get("stream"))
        # The `claude` CLI injects newer top-level fields (e.g.
        # `context_management`, a context-editing beta) that the Vertex
        # `anthropic_version: vertex-2023-10-16` endpoint rejects with
        # "Extra inputs are not permitted". Drop anything outside the set of
        # fields the Vertex Messages endpoint accepts.
        is_anthropic_model = model.startswith("claude-") and not any(
            tag in model for tag in ("glm-", "deepseek-", "minimax-", "kimi-")
        )
        if is_anthropic_model:
            for k in list(payload):
                if k not in _VERTEX_ALLOWED_FIELDS:
                    payload.pop(k, None)
            _inject_cache_control(payload)
            payload["anthropic_version"] = ANTHROPIC_VERTEX_VERSION
        else:
            payload.pop("anthropic_version", None)
            if "reasoning_effort" not in payload and "thinking" not in payload:
                payload["reasoning_effort"] = os.environ.get(
                    "NONANTHRO_REASONING_EFFORT", "high"
                )

        req = Request(
            vertex_url(model, stream),
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        try:
            resp = urlopen(req, timeout=600)
        except HTTPError as e:  # forward upstream error verbatim
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except URLError as e:
            return self._err(502, f"upstream unreachable: {e}")

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        self.end_headers()
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            _track_cache(chunk)
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break


_cache_stats = {"requests": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0, "input_tokens": 0}
_STATS_INTERVAL = 20  # print summary every N requests


def _track_cache(chunk: bytes) -> None:
    """Parse usage from non-streaming response or SSE message_stop to track cache."""
    try:
        text = chunk.decode("utf-8", "ignore")
        for line in text.splitlines():
            if '"usage"' not in line:
                continue
            # strip SSE prefix if present
            if line.startswith("data: "):
                line = line[6:]
            obj = json.loads(line)
            usage = obj.get("usage", {})
            if not usage:
                continue
            _cache_stats["requests"] += 1
            _cache_stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            _cache_stats["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
            _cache_stats["input_tokens"] += usage.get("input_tokens", 0)
            if _cache_stats["requests"] % _STATS_INTERVAL == 0:
                total_in = _cache_stats["input_tokens"]
                cr = _cache_stats["cache_read_tokens"]
                cc = _cache_stats["cache_creation_tokens"]
                pct = f"{cr/(total_in+cr+cc)*100:.1f}%" if (total_in+cr+cc) > 0 else "n/a"
                print(f"[cache stats] reqs={_cache_stats['requests']}  "
                      f"cache_read={cr:,}  cache_create={cc:,}  input={total_in:,}  "
                      f"hit_rate={pct}", flush=True)
    except Exception:
        pass


class _Server(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4112)
    args = ap.parse_args()
    if not TOKEN:
        print("ERROR: set VERTEX_PROXY_TOKEN (or CRESTA_VERTEX_TOKEN)", file=sys.stderr)
        sys.exit(1)
    print(f"anthropic->vertex shim on http://127.0.0.1:{args.port}  ->  {BASE} "
          f"({PROJECT}/{LOCATION}); token …{TOKEN[-6:]}", flush=True)
    _Server(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
