"""Reasoning effort and forced final answers.

Reasoning effort is sent as the standard `reasoning_effort` request field. A llama.cpp server hands it to the chat
template, which says whether it knows the setting (`chat_template_caps.supports_reasoning_effort`), which values it
accepts (rendering an unknown value fails with a message listing them) and which one it uses when none is given.

A forced final answer is for a reply that hit the output token limit while still reasoning: instead of recording
an empty answer, the model is made to answer from the reasoning it had done.

- On llama.cpp the prompt is rendered by the server's own chat template, the cut-off reasoning is put back where the
  model left it, followed by a short note that time is up and the template's end-of-reasoning marker, and the model
  continues from there (`/completion`). It answers with its full working in context: budget forcing.
- Elsewhere (hosted chat APIs do not let a client write into the model's reasoning) the reasoning is handed back in
  a follow-up message that asks for the final answer now.

Either way the answer is recorded as its own step with finish_reason "forced", and graded like any other answer.
"""

from __future__ import annotations

import json
import re

from .clients import ApiError, http_json

FORCE_TOKENS = 4096
FORCE_NOTE = "\n\nI have run out of thinking time, so I must stop here and give my final answer now."
FOLLOWUP = ("Your thinking budget for this reply is used up, so your reasoning was cut off. This is what you had "
            "worked out so far:\n\n<reasoning>\n{reasoning}\n</reasoning>\n\nDo not continue the analysis. Give "
            "your final answer now, exactly in the format that was requested.")
FOLLOWUP_CHARS = 60000  # the end of the reasoning is where the conclusions are
SENTINEL_R, SENTINEL_C = "QBREASONINGQB", "QBCONTENTQB"
DEFAULT_RE = re.compile(r"reasoning_effort\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]")


def template_reasoning(props: dict) -> dict:
    """What a llama.cpp /props answer says about reasoning effort."""
    caps = props.get("chat_template_caps") or {}
    template = props.get("chat_template") or ""
    m = DEFAULT_RE.search(template)
    return {"supports_effort": caps.get("supports_reasoning_effort"), "default_effort": m.group(1) if m else None}


def _headers(api_key: str | None) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def apply_template(root: str, api_key: str | None, body: dict) -> str:
    data = http_json(root + "/apply-template", body, _headers(api_key), timeout=120)
    if not isinstance(data, dict) or not isinstance(data.get("prompt"), str):
        raise ApiError(f"unexpected /apply-template answer: {json.dumps(data)[:300]}")
    return data["prompt"]


def check_effort(root: str, api_key: str | None, model: str, effort: str) -> str | None:
    """None if the model's chat template accepts `effort`, else the server's message (it lists the values)."""
    try:
        apply_template(root, api_key, {"model": model, "messages": [{"role": "user", "content": "Hi"}],
                                       "reasoning_effort": effort})
        return None
    except ApiError as e:
        return str(e)


def closing_sequence(root: str, api_key: str | None, model: str, extra_body: dict) -> str | None:
    """The text the chat template puts between the end of the reasoning and the answer (e.g. "\\n</think>\\n\\n"),
    found by rendering a finished exchange; None if the template does not show reasoning that way."""
    body = {"model": model, "add_generation_prompt": False, **extra_body,
            "messages": [{"role": "user", "content": "Hi"},
                         {"role": "assistant", "reasoning_content": SENTINEL_R, "content": SENTINEL_C}]}
    try:
        prompt = apply_template(root, api_key, body)
    except ApiError:
        return None
    start, end = prompt.find(SENTINEL_R), prompt.find(SENTINEL_C)
    if start < 0 or end < 0 or end < start:
        return None
    closing = prompt[start + len(SENTINEL_R):end]
    return closing if closing.strip() else None


def continue_reasoning(conv, root: str, api_key: str | None, reasoning: str, closing: str) -> tuple[str, int]:
    """llama.cpp: continue the model's own cut-off reasoning with a time-is-up note and let it answer."""
    messages = ([{"role": "system", "content": conv.system}] if conv.system else []) + conv.messages[:-1]
    body = {"model": conv.model, "messages": messages, **conv.extra_body}
    if conv.tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": json.loads(t["parameters_json"])}}
                         for t in conv.tools]
    prompt = apply_template(root, api_key, body) + reasoning.rstrip() + FORCE_NOTE + closing
    data = http_json(root + "/completion", {"model": conv.model, "prompt": prompt, "n_predict": FORCE_TOKENS,
                                            **conv.sampling},
                     _headers(api_key), conv.timeout, retries=3)
    if not isinstance(data, dict):
        raise ApiError(f"unexpected /completion answer: {json.dumps(data)[:300]}")
    return (data.get("content") or "").strip(), int(data.get("tokens_predicted") or 0)


# How to get an answer out of a reasoning model in the follow-up, tried in order until one gives text: as is; with
# thinking switched off the chat-template way (llama.cpp, vLLM) or the OpenRouter way; then, for models whose
# reasoning cannot be switched off, at low effort with more room. A variant the API rejects is skipped.
FOLLOWUP_VARIANTS = [
    ("follow-up", {}, FORCE_TOKENS),
    ("follow-up, thinking off", {"chat_template_kwargs": {"enable_thinking": False}}, FORCE_TOKENS),
    ("follow-up, thinking off", {"reasoning": {"enabled": False}}, FORCE_TOKENS),
    ("follow-up, low effort", {"reasoning": {"effort": "low"}}, 4 * FORCE_TOKENS),
    ("follow-up, low effort", {"reasoning_effort": "low"}, 4 * FORCE_TOKENS),
]


def _merge(body: dict, extra: dict) -> dict:
    out = dict(body)
    for key, value in extra.items():
        out[key] = {**(out.get(key) or {}), **value} if isinstance(value, dict) else value
    return out


def follow_up(conv, reasoning: str) -> tuple[str, int, str]:
    """Any API: ask for the final answer in a follow-up message that hands the reasoning back. Returns the answer,
    the tokens spent and the variant that produced it."""
    n, saved_tokens, saved_body = len(conv.messages), conv.max_tokens, conv.extra_body
    tokens, text, method = 0, "", FOLLOWUP_VARIANTS[0][0]
    try:
        for i, (label, extra, budget) in enumerate(FOLLOWUP_VARIANTS):
            del conv.messages[n:]
            conv.add_user(FOLLOWUP.format(reasoning=reasoning[-FOLLOWUP_CHARS:]))
            conv.max_tokens, conv.extra_body = budget, _merge(saved_body, extra)
            try:
                step = conv.complete()
            except ApiError:
                if i == 0:
                    raise  # the plain request failing is a real error
                continue
            tokens += step.output_tokens
            text, method = step.text.strip(), label
            if text:
                break
    finally:
        del conv.messages[n:]  # the conversation goes on as if the model had answered directly
        conv.max_tokens, conv.extra_body = saved_tokens, saved_body
    return text, tokens, method


def forced_empty(step: dict) -> bool:
    """A forced answer that came back empty (worth forcing again)."""
    return step.get("finish_reason") == "forced" and not (step.get("text") or "").strip()


def needs_forcing(step: dict) -> bool:
    """A reply cut off at the token limit while still reasoning: no answer, no tool call, but reasoning."""
    return (step.get("finish_reason") == "length" and not step.get("tool_calls")
            and not (step.get("text") or "").strip() and bool((step.get("reasoning") or "").strip()))


def force_answer(conv, endpoint: dict, api_key: str | None, reasoning: str) -> dict:
    """Make the model answer from its cut-off reasoning; returns the step to record."""
    closing = endpoint.get("closing")
    if closing:
        text, tokens = continue_reasoning(conv, endpoint["root"], api_key, reasoning, closing)
        method = "continuation"
    else:
        text, tokens, method = follow_up(conv, reasoning)
    # Later turns see the forced answer as the model's reply.
    conv.messages[-1] = {"role": "assistant", "content": text}
    return {"text": text, "reasoning": "", "tool_calls": [], "finish_reason": "forced", "forced": {"method": method},
            "output_tokens": tokens, "reasoning_tokens": 0, "timings": {}}
