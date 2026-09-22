"""OpenAI- and Anthropic-style chat clients, normalized to a common step result.

Endpoints are always built from the user-supplied base URL; there is deliberately
no default URL anywhere in this module.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API_STYLES = ("openai", "anthropic")

THINK_RE = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)


class ApiError(Exception):
    """The server answered with an error; recorded as a failed response for the problem."""


class RunAborted(Exception):
    """Nothing sensible can be recorded for any problem; aborts the run."""


class ConnectionFailed(RunAborted):
    """The server could not be reached."""


class MaxTokensRequired(RunAborted):
    """The endpoint insists on an output token limit, and none was given."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | None  # None when the model produced unparseable arguments
    arguments_raw: str


@dataclass
class StepResult:
    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: str  # "stop" | "length" | "tool_calls" | other server-specific value
    prompt_tokens: int
    output_tokens: int
    reasoning_tokens: int | None  # None when the API does not report it
    model: str | None
    timings: dict = field(default_factory=dict)


def normalize_base_url(url: str) -> str:
    """Server root without a trailing slash or /v1 suffix."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url.rstrip("/")


def http_json(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 30,
              retries: int = 0) -> dict:
    """GET (payload None) or POST JSON; returns the decoded JSON body."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Overload / rate limit are worth retrying; other statuses are the request's fault.
            if e.code not in (429, 502, 503, 529) or last:
                raise ApiError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}") from e
        except json.JSONDecodeError as e:
            raise ApiError(f"response is not JSON: {e}") from e
        except TimeoutError as e:
            # Read timeout while generating: retrying would just run into it again.
            raise ApiError(f"no response within {timeout:.0f}s") from e
        except (urllib.error.URLError, ConnectionError) as e:
            if last:
                raise ConnectionFailed(f"{url}: {e}") from e
        time.sleep(min(2 ** (attempt + 1), 30))
    raise AssertionError("unreachable")


def http_stream(url: str, payload: dict, headers: dict, timeout: float):
    """POST and yield the JSON objects of a server-sent event stream ("data: {...}" lines).

    The timeout is per read, i.e. an idle timeout: a generation may take hours as long as tokens keep coming.
    """
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json", "Accept": "text/event-stream",
                                      **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError as e:
                    raise ApiError(f"bad stream chunk: {data[:200]}") from e
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}") from e
    except TimeoutError as e:
        raise ApiError(f"stream idle for {timeout:.0f}s") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise ConnectionFailed(f"{url}: {e}") from e


class Conversation:
    """One conversation with the model under test. Subclasses speak a specific API style."""

    path = ""

    def __init__(self, root: str, model: str, api_key: str | None, system: str | None, tools: list[dict],
                 max_tokens: int | None, sampling: dict, extra_body: dict, timeout: float,
                 stream: bool = False):
        self.url = root + self.path
        self.stream = stream
        self.model = model
        self.api_key = api_key
        self.system = system
        self.tools = tools
        self.max_tokens = max_tokens
        self.sampling = sampling
        self.extra_body = extra_body
        self.timeout = timeout
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def complete(self) -> StepResult:
        raise NotImplementedError

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        raise NotImplementedError

    def _post(self, payload: dict, headers: dict) -> dict:
        return http_json(self.url, {**payload, **self.sampling, **self.extra_body}, headers, self.timeout, retries=3)


class OpenAIConversation(Conversation):
    path = "/v1/chat/completions"
    # Newer OpenAI models reject max_tokens; switch once the server tells us so.
    max_tokens_param = "max_tokens"

    def complete(self) -> StepResult:
        messages = ([{"role": "system", "content": self.system}] if self.system else []) + self.messages
        payload = {"model": self.model, "messages": messages}
        if self.tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": json.loads(t["parameters_json"])}}
                for t in self.tools
            ]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if self.stream:
            data = self._stream(payload, headers)
        elif self.max_tokens is None:  # no limit: generation ends when the model stops or the context is full
            data = self._post(payload, headers)
        else:
            try:
                data = self._post({**payload, self.max_tokens_param: self.max_tokens}, headers)
            except ApiError as e:
                if "max_completion_tokens" not in str(e) or self.max_tokens_param != "max_tokens":
                    raise
                OpenAIConversation.max_tokens_param = "max_completion_tokens"
                data = self._post({**payload, "max_completion_tokens": self.max_tokens}, headers)

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise ApiError(f"unexpected response shape: {json.dumps(data)[:500]}") from e

        # Echo the assistant message back as received so templates with interleaved
        # thinking still see their reasoning during tool loops.
        stored = {k: v for k, v in message.items() if v is not None}
        if not stored.get("content") and not stored.get("tool_calls"):
            # A reasoning-only (cut off) turn has no content; llama.cpp refuses to
            # replay an assistant message without content or tool calls.
            stored["content"] = ""
        self.messages.append(stored)

        text = message.get("content") or ""
        if isinstance(text, list):  # content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if not text and message.get("refusal"):
            text = message["refusal"]
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if not reasoning:
            m = THINK_RE.match(text)
            if m:
                reasoning, text = m.group(1).strip(), text[m.end():]

        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or ""
            calls.append(ToolCall(tc.get("id") or "", fn.get("name") or "", _parse_arguments(raw),
                                  raw if isinstance(raw, str) else json.dumps(raw)))

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return StepResult(
            text=text,
            reasoning=reasoning,
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            reasoning_tokens=details.get("reasoning_tokens"),
            model=data.get("model"),
            timings=data.get("timings") or {},
        )

    def _stream(self, payload: dict, headers: dict) -> dict:
        """Streamed request, returned in the shape of a non-streamed chat completion."""
        body = {**payload, **self.sampling, **self.extra_body, "stream": True,
                "stream_options": {"include_usage": True}}
        if self.max_tokens is not None:
            body[self.max_tokens_param] = self.max_tokens
        text, reasoning, finish, usage, model = [], [], None, None, None
        calls: dict[int, dict] = {}
        for chunk in http_stream(self.url, body, headers, self.timeout):
            if chunk.get("usage"):
                usage = chunk["usage"]
            model = chunk.get("model") or model
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                if delta.get("refusal"):  # the provider refused; keep its text so the record is not just empty
                    text.append(delta["refusal"])
                for key in ("reasoning_content", "reasoning"):
                    if delta.get(key):
                        reasoning.append(delta[key])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)),
                                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    args = fn.get("arguments") or ""
                    slot["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
                finish = choice.get("finish_reason") or finish
        message = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"choices": [{"message": message, "finish_reason": finish or "stop"}], "usage": usage or {},
                "model": model}

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})


class AnthropicConversation(Conversation):
    path = "/v1/messages"
    STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}

    def complete(self) -> StepResult:
        payload = {"model": self.model, "messages": self.messages}
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.system:
            payload["system"] = self.system
        if self.tools:
            payload["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": json.loads(t["parameters_json"])}
                for t in self.tools
            ]
        headers = {"anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            data = self._post(payload, headers)
        except ApiError as e:
            # The Anthropic API proper makes max_tokens mandatory; llama.cpp does not.
            if self.max_tokens is None and "max_tokens" in str(e):
                raise MaxTokensRequired("this endpoint requires an output token limit; pass --max-tokens") from e
            raise

        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ApiError(f"unexpected response shape: {json.dumps(data)[:500]}")
        self.messages.append({"role": "assistant", "content": blocks})

        text, reasoning, calls = [], [], []
        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                text.append(block.get("text", ""))
            elif btype == "thinking":
                reasoning.append(block.get("thinking", ""))
            elif btype == "tool_use":
                args = block.get("input")
                calls.append(ToolCall(block.get("id") or "", block.get("name") or "",
                                      args if isinstance(args, dict) else None, json.dumps(args)))

        usage = data.get("usage") or {}
        stop = data.get("stop_reason") or "end_turn"
        return StepResult(
            text="".join(text),
            reasoning="\n".join(reasoning),
            tool_calls=calls,
            finish_reason=self.STOP_REASONS.get(stop, stop),
            prompt_tokens=sum(usage.get(k) or 0 for k in
                              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
            output_tokens=usage.get("output_tokens", 0),
            reasoning_tokens=None,
            model=data.get("model"),
        )

    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.id, "content": result} for call, result in results],
        })


def _parse_arguments(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


CONVERSATIONS = {"openai": OpenAIConversation, "anthropic": AnthropicConversation}
