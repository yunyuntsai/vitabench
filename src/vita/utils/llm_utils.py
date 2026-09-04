import json
import os
import random
import re
import time
from typing import Any, Optional

from loguru import logger
import requests

# Dedicated RNG for retry backoff jitter. Kept separate from the global
# `random` stream that simulations seed (random.seed(seed)) so jitter never
# perturbs reproducible simulation state.
_jitter_rng = random.Random(20240819)


from vita.config import (
    models,
    DEFAULT_MAX_RETRIES,
)
from vita.data_model.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from vita.environment.tool import Tool


class DictToObject:
    """
    Convert dictionary to object with attribute access
    Usage:
    response_obj = DictToObject(response)
    print(response_obj.choices[0].message.content)  # Instead of response["choices"][0]["message"]["content"]
    """
    def __init__(self, dictionary):
        for key, value in dictionary.items():
            if isinstance(value, dict):
                setattr(self, key, DictToObject(value))
            elif isinstance(value, list):
                setattr(self, key, [DictToObject(item) if isinstance(item, dict) else item for item in value])
            else:
                setattr(self, key, value)

    def to_dict(self):
        """Convert object back to dictionary"""
        result = {}
        for key, value in self.__dict__.items():
            if isinstance(value, DictToObject):
                result[key] = value.to_dict()
            elif isinstance(value, list):
                result[key] = [item.to_dict() if isinstance(item, DictToObject) else item for item in value]
            else:
                result[key] = value
        return result


def get_response_cost(usage, model) -> float:
    num_prompt_token = usage["prompt_tokens"]
    num_completion_token = usage["completion_tokens"]
    prompt_price = models.get(model, {}).get("cost_1m_token_dollar",{}).get("prompt_price", 0)
    completion_price = models.get(model, {}).get("cost_1m_token_dollar",{}).get("completion_price", 0)
    if prompt_price and completion_price:
        return (prompt_price * num_prompt_token + completion_price * num_completion_token) / 1000000
    else:
        return 0.0


def get_response_usage(response) -> Optional[dict]:
    usage = response.get("usage", {})
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0)
    }


def format_messages(messages: list[Message]) -> list[dict]:
    messages_formatted = []
    for message in messages:
        if isinstance(message, UserMessage):
            messages_formatted.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            tool_calls = None
            if message.is_tool_call():
                tool_calls = [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                        "type": "function",
                    }
                    for tc in message.tool_calls
                ]
            messages_formatted.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": tool_calls,
                }
            )
            # add interleaved thinking content if exists
            if message.raw_data is not None and message.raw_data.get("message") is not None:
                reasoning_content = message.raw_data["message"].get("reasoning_content")
                if reasoning_content:
                    messages_formatted[-1]["reasoning_content"] = reasoning_content
        elif isinstance(message, ToolMessage):
            messages_formatted.append(
                {
                    "role": "tool",
                    "content": message.content,
                    "tool_call_id": message.id,
                    "name": message.name,
                }
            )
        elif isinstance(message, SystemMessage):
            messages_formatted.append({"role": "system", "content": message.content})
    return messages_formatted


def to_claude_think_official(messages_formatted: list[dict], messages: list[Message]) -> list[dict]:
    try:
        for i, (formatted_msg, original_msg) in enumerate(zip(messages_formatted, messages)):
            if formatted_msg.get("role") != "assistant":
                continue

            if not hasattr(original_msg, 'raw_data') or original_msg.raw_data is None:
                continue

            raw_message = original_msg.raw_data.get("message", {})

            # Extract reasoning content and signature
            reasoning_content = raw_message.get("reasoning_content") or raw_message.get("reasoning")
            signature = raw_message.get("signature")
            if reasoning_content:
                messages_formatted[i]["reasoning_content"] = reasoning_content
            if signature:
                messages_formatted[i]["signature"] = signature

    except Exception as e:
        import traceback
        traceback.print_exc()

    return messages_formatted


def to_deepseek_think_official(messages_formatted: list[dict], messages: list[Message]) -> list[dict]:
    try:
        for i, (formatted_msg, original_msg) in enumerate(zip(messages_formatted, messages)):
            if formatted_msg.get("role") != "assistant":
                continue

            if not hasattr(original_msg, 'raw_data') or original_msg.raw_data is None:
                continue

            reasoning_content = original_msg.raw_data.get("message", {}).get("reasoning_content", None) or \
                               original_msg.raw_data.get("message", {}).get("reasoning", None)

            if reasoning_content:
                messages_formatted[i]["reasoning_content"] = reasoning_content

    except Exception as e:
        import traceback
        traceback.print_exc()

    return messages_formatted


# Config-only keys from models.yaml. OpenAI rejects these if they appear in the JSON body.
_REQUEST_META_KEYS = {
    "base_url",
    "headers",
    "cost_1m_token_dollar",
    "name",
    "max_input_tokens",
    "api_model",
    "api_style",
    "thinking",
}


def kwargs_adapter(data: dict, enable_think: False, messages: list) -> dict:
    if "claude" in data["model"]:
        if not enable_think:
            data["thinking"] = {"type": "disabled"}
        else:
            data["messages"] = to_claude_think_official(data["messages"], messages)
    elif "deepseek" in data["model"]:
        if enable_think:
            data["messages"] = to_deepseek_think_official(data["messages"], messages)
    else:
        model_name = data.get("model", "")
        if model_name.startswith("gpt-5"):
            if "max_tokens" in data:
                if "max_completion_tokens" not in data:
                    data["max_completion_tokens"] = data.pop("max_tokens")
                else:
                    data.pop("max_tokens")
            if not enable_think:
                if model_name == "gpt-5":
                    data["reasoning_effort"] = "minimal"
                elif "reasoning_effort" not in data:
                    data["reasoning_effort"] = "none"
            elif "reasoning_effort" not in data:
                data["reasoning_effort"] = "high"
        elif not enable_think and "reasoning_effort" in data:
            data.pop("reasoning_effort")
    return data


def _split_request(data: dict) -> tuple[str, dict, dict]:
    base_url = data.get("base_url")
    if not base_url:
        raise ValueError("models.yaml is missing default.base_url")
    headers = data.get("headers") or {}
    body = {
        key: value
        for key, value in data.items()
        if key not in _REQUEST_META_KEYS and value is not None
    }
    return base_url, headers, body


# --- Bedrock Anthropic-native path (api_style: bedrock_anthropic) ------------
# claude-sonnet-4-5 judge is reached via Cresta's /proxy/bedrock, which speaks
# the Anthropic-native Bedrock invoke API (POST /model/<id>/invoke) and requires
# a valid AWS SigV4 signature (the proxy authorizes the model itself via the
# x-api-key Claude-Code token; the SigV4 only has to be a well-formed signature).
# generate() otherwise speaks OpenAI format, so these helpers convert
# OpenAI-format messages <-> Anthropic-native and re-shape the Anthropic response
# into the OpenAI `choices[0].message` shape the existing parser expects.

_BEDROCK_AWS = None  # (boto3.Session, region) cache


def _bedrock_aws_creds():
    global _BEDROCK_AWS
    if _BEDROCK_AWS is None:
        import boto3
        profile = os.environ.get("AWS_PROFILE", "dev-servers_dev")
        region = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_REGION", "us-west-2"))
        _BEDROCK_AWS = (boto3.Session(profile_name=profile), region)
    session, region = _BEDROCK_AWS
    return session.get_credentials(), region


def _merge_anthropic_content(a, b):
    """Merge two Anthropic message `content` values (str or list of blocks)."""
    a_blocks = a if isinstance(a, list) else ([{"type": "text", "text": a}] if a else [])
    b_blocks = b if isinstance(b, list) else ([{"type": "text", "text": b}] if b else [])
    return a_blocks + b_blocks


def _openai_to_anthropic_body(data: dict, enable_think: bool) -> dict:
    """Convert an OpenAI-format chat request into an Anthropic-native Bedrock body."""
    msgs = data.get("messages") or []
    system_parts: list[str] = []
    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        if role == "system":
            c = m.get("content")
            if c:
                system_parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
            continue
        if role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") or "",
            }]})
            continue
        if role == "assistant":
            blocks: list = []
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                try:
                    args_obj = json.loads(args) if isinstance(args, str) else (args or {})
                except Exception:
                    args_obj = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "toolu_x",
                               "name": fn.get("name") or "", "input": args_obj})
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
            continue
        # user (default)
        c = m.get("content")
        out.append({"role": "user", "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})

    # Anthropic requires strictly alternating user/assistant turns starting with user.
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] = _merge_anthropic_content(merged[-1]["content"], m["content"])
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "."})

    max_tokens = data.get("max_tokens") or data.get("max_completion_tokens") or 1024
    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": merged,
        "max_tokens": max_tokens,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if data.get("temperature") is not None:
        body["temperature"] = data.get("temperature")
    th = data.get("thinking")
    if th and th.get("type") == "enabled" and enable_think:
        budget = th.get("budget_tokens") or 16000
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if body["max_tokens"] <= budget:
            body["max_tokens"] = budget + 4096
    return body


def _anthropic_to_openai(resp: dict) -> dict:
    """Re-shape an Anthropic-native response into the OpenAI `choices[0]` form."""
    blocks = resp.get("content") or []
    text_parts, tool_calls = [], []
    for b in blocks:
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id"), "type": "function",
                "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
            })
    usage = resp.get("usage") or {}
    stop = resp.get("stop_reason")
    finish = {"end_turn": "stop", "stop_sequence": "stop",
              "max_tokens": "length", "tool_use": "tool_calls"}.get(stop, "stop")
    message: dict = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)},
    }


def _generate_bedrock_anthropic(data: dict, headers: dict, enable_think: bool) -> dict:
    """POST an Anthropic-native Bedrock invoke (SigV4-signed) and return an OpenAI-shaped response dict."""
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4Auth

    creds, region = _bedrock_aws_creds()
    base_url = data["base_url"].rstrip("/")
    api_model = data.get("api_model") or data["model"]
    url = f"{base_url}/model/{api_model}/invoke"
    body = json.dumps(_openai_to_anthropic_body(data, enable_think), ensure_ascii=False)

    base_headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    xak = headers.get("x-api-key") or headers.get("X-Api-Key") or os.environ.get("ANTHROPIC_API_KEY")
    if xak:
        base_headers["x-api-key"] = xak

    max_retries = 6
    retry_delay = 2.0
    last_snippet = ""
    for attempt in range(max_retries + 1):
        req = AWSRequest(method="POST", url=url, data=body, headers=dict(base_headers))
        try:
            SigV4Auth(creds, "bedrock", region).add_auth(req)
        except Exception as e:
            # Most likely an expired SSO session — surface clearly, no point retrying.
            raise ValueError(f"Bedrock SigV4 signing failed (refresh AWS SSO? `aws sso login --profile "
                             f"{os.environ.get('AWS_PROFILE','dev-servers_dev')}`): {e}")
        try:
            resp = requests.post(url, data=body, headers=dict(req.headers), timeout=(10, 180))
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                wait = min(retry_delay * _jitter_rng.uniform(0.75, 1.25), 30.0)
                logger.warning(f"Bedrock request exception, attempt {attempt + 1}/{max_retries + 1}, retry in {wait:.1f}s: {e}")
                time.sleep(wait); retry_delay = min(retry_delay * 2, 30.0); continue
            raise
        status = resp.status_code
        try:
            rj = resp.json()
        except ValueError:
            rj = None
        retryable = status == 429 or status >= 500 or (
            not isinstance(rj, dict) or ("content" not in rj and "error" in rj))
        if retryable:
            try:
                last_snippet = json.dumps(rj if rj is not None else resp.text)[:300]
            except Exception:
                last_snippet = "<unparseable>"
            if attempt < max_retries:
                wait = min(retry_delay * _jitter_rng.uniform(0.75, 1.25), 30.0)
                logger.warning(f"Bedrock transient error (status={status}), attempt {attempt + 1}/{max_retries + 1}, retry in {wait:.1f}s: {last_snippet}")
                time.sleep(wait); retry_delay = min(retry_delay * 2, 30.0); continue
            raise ValueError(f"Bedrock returned retryable error after {max_retries + 1} attempts (status={status}): {last_snippet}")
        if not isinstance(rj, dict) or "content" not in rj:
            raise ValueError(f"Bedrock returned unexpected response (status={status}): {json.dumps(rj, ensure_ascii=False)[:300]}")
        return _anthropic_to_openai(rj)


def generate(
    model: str,
    messages: list[Message],
    tools: Optional[list[Tool]] = None,
    tool_choice: Optional[str] = None,
    enable_think: bool = False,
    **kwargs: Any,
) -> UserMessage | AssistantMessage:
    """
    Generate a response from the model.

    Args:
        model: The model to use.
        messages: The messages to send to the model.
        tools: The tools to use.
        tool_choice: The tool choice to use.
        enable_think: Whether to enable think mode for the agent.
        **kwargs: Additional arguments to pass to the model.

    Returns: A tuple containing the message and the cost.
    """
    try:
        if kwargs.get("num_retries") is None:
            kwargs["num_retries"] = DEFAULT_MAX_RETRIES
        messages_formatted = format_messages(messages)
        tools = [tool.openai_schema for tool in tools] if tools else None
        if tools and tool_choice is None:
            tool_choice = "auto"
        try:
            data = {
                "model": model,
                "messages": messages_formatted,
                "stream": False,
                "temperature": kwargs.get("temperature"),
                "tools": tools,
                "tool_choice": tool_choice,
            }
            data.update(models[model])
            # Optional per-model request-body model-id override. A yaml entry may be
            # named for display (e.g. "qwen3.8-max") but must send a different id to
            # its provider (e.g. "accounts/fireworks/models/qwen3p8-max"). Keyed as
            # `api_model` (not `model`) so it does not collide with the `model` named
            # parameter when the merged config dict is spread as **llm_args into
            # generate(). No-op for entries without `api_model`.
            if data.get("api_model"):
                data["model"] = data.pop("api_model")
            data = kwargs_adapter(data, enable_think, messages)
            base_url, headers, body = _split_request(data)

            # Anthropic-native Bedrock path (claude judge): convert + SigV4-sign +
            # POST to /model/<id>/invoke, return an OpenAI-shaped response so the
            # parser below is shared with the OpenAI path.
            if data.get("api_style") == "bedrock_anthropic":
                response = _generate_bedrock_anthropic(data, headers, enable_think)
            else:
                max_retries = 6
                retry_delay = 2.0
                response = None
                for attempt in range(max_retries + 1):
                    try:
                        # read timeout lowered from 600s to 120s for infra robustness only:
                        # a hung read now fails fast and retries (below) instead of blocking
                        # 40+ min per call. No scoring impact — a timed-out turn retries and
                        # returns the same response. (infra-hardening, not an optimization edit)
                        resp = requests.post(base_url, json=body, headers=headers, timeout=(10, 120))

                        # Classify retryable provider errors. Fireworks (and OpenAI) can
                        # return rate-limit / capacity errors either as HTTP 429/5xx or as
                        # a 2xx body shaped like {"error": {...}} with no "choices". Retry
                        # those with backoff+jitter so concurrent sweeps sharing a provider
                        # quota drain at the allowed rate instead of failing the task.
                        status = resp.status_code
                        body_json = None
                        retryable = False
                        if status == 429 or status >= 500:
                            retryable = True
                        else:
                            try:
                                body_json = resp.json()
                            except ValueError:
                                body_json = None
                            if not isinstance(body_json, dict) or (
                                "choices" not in body_json and "error" in body_json
                            ):
                                retryable = True

                        if retryable:
                            if attempt < max_retries:
                                wait = min(retry_delay * _jitter_rng.uniform(0.75, 1.25), 30.0)
                                logger.warning(
                                    f"Provider transient error (status={status}), "
                                    f"attempt {attempt + 1}/{max_retries + 1}, retrying in {wait:.1f}s"
                                )
                                time.sleep(wait)
                                retry_delay = min(retry_delay * 2, 30.0)
                                continue
                            # Exhausted retries: surface a clear error with a body snippet.
                            snippet = ""
                            try:
                                snippet = json.dumps(body_json if body_json is not None else resp.text)[:300]
                            except Exception:
                                snippet = "<unparseable>"
                            raise ValueError(
                                f"Provider returned retryable error after {max_retries + 1} attempts "
                                f"(status={status}): {snippet}"
                            )

                        response = body_json if body_json is not None else resp.json()
                        break

                    except requests.exceptions.RequestException as e:
                        if attempt < max_retries:
                            wait = min(retry_delay * _jitter_rng.uniform(0.75, 1.25), 30.0)
                            logger.warning(f"Request exception, attempt {attempt + 1} retry, retrying in {wait:.1f}s. Error: {e}")
                            time.sleep(wait)
                            retry_delay = min(retry_delay * 2, 30.0)
                        else:
                            raise e
        except Exception as e:
            logger.error(e)
            raise e
        usage = get_response_usage(response)
        cost = get_response_cost(usage, model)
        try:
            response = response['choices'][0]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Full response: {json.dumps(response, ensure_ascii=False, indent=2) if isinstance(response, dict) else response}")
            raise ValueError(f"Invalid API response format: {e}") from e
        assert response['message']['role'] == "assistant", (
            "The response should be an assistant message"
        )
        content = response['message'].get('content')
        tool_calls = response['message'].get('tool_calls') or []
        tool_calls = [
            ToolCall(
                id=tool_call.get('id'),
                name=tool_call.get('function', {}).get('name'),
                arguments=json.loads(tool_call.get('function', {}).get('arguments')) if tool_call.get('function', {}).get('arguments') else {},
            )
            for tool_call in tool_calls
        ]
        tool_calls = tool_calls or None
        message = AssistantMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            cost=cost,
            usage=usage,
            raw_data=response,
        )
        return message
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(e)
        raise


def get_cost(messages: list[Message]) -> tuple[float, float] | None:
    """
    Get the cost of the interaction between the agent and the user.
    Returns None if any message has no cost.
    """
    agent_cost = 0
    user_cost = 0
    for message in messages:
        if isinstance(message, ToolMessage):
            continue
        if message.cost is not None:
            if isinstance(message, AssistantMessage):
                agent_cost += message.cost
            elif isinstance(message, UserMessage):
                user_cost += message.cost
        else:
            logger.warning(f"Message {message.role}: {message.content} has no cost")
            return None
    return agent_cost, user_cost
