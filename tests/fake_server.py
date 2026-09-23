"""A tiny llama.cpp-like server speaking both API styles, for tests.

Behaviour: if the request offers tools and no tool result is in the conversation yet, the
model "calls" the first tool with {"order_id": "A-1"}; otherwise it answers with text.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATH = "/models/Swift-Qwen3.8-27B-Uncensored-MTP-Q8_0.gguf"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        router = getattr(self.server, "router_models", None)
        if router is not None:
            return self._router_get(router)
        if self.path == "/props" and self.server.llamacpp:
            self._send(200, {"model_path": self.server.model_path, "model_ftype": "Q8_0",
                             "build_info": "b11023-4ff829ec2",
                             "chat_template": "{%- set reasoning_effort = reasoning_effort|default('xhigh') %}",
                             "chat_template_caps": {"supports_reasoning_effort": True},
                             "default_generation_settings": {"n_ctx": 4096, "params": {"temperature": 0.7}}})
        elif self.path == "/v1/models":
            model_id = self.server.model_path if self.server.llamacpp else "served-model-name"
            self._send(200, {"data": [{"id": model_id, "meta": {"n_params": 27000000000}}]})
        else:
            self._send(404, {"error": "not found"})

    def _router_get(self, models: dict) -> None:
        """llama.cpp router mode: /props describes the router, /props?model=X one of its models."""
        if self.path == "/props":
            self._send(200, {"role": "router", "model_path": "none", "build_info": "b11023-4ff829ec2"})
        elif self.path.startswith("/props?model="):
            name = self.path.split("=", 1)[1]
            if name not in models:
                return self._send(404, {"error": "model not found"})
            self._send(200, {"model_path": models[name]["path"], "model_ftype": "F16", "model_alias": name,
                             "default_generation_settings": {"n_ctx": 4096, "params": {}}})
        elif self.path == "/v1/models":
            self._send(200, {"data": [{"id": name, "meta": {"n_params": 123},
                                       "status": {"value": "loaded", "args": m["args"]}}
                                      for name, m in models.items()]})
        else:
            self._send(404, {"error": "not found"})

    def _stream_chat(self, body: dict) -> None:
        """The same fake model as the non-streamed path, delivered as server-sent events in small deltas."""
        has_result = any(m["role"] == "tool" for m in body["messages"])
        chunks = [{"delta": {"role": "assistant", "reasoning_content": "let me "}},
                  {"delta": {"reasoning_content": "think about it"}}]
        if body.get("tools") and not has_result:
            name = body["tools"][0]["function"]["name"]
            chunks += [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                   "function": {"name": name, "arguments": ""}}]}},
                       {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"order_'}}]}},
                       {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'id": "A-1"}'}}]}},
                       {"delta": {}, "finish_reason": "tool_calls"}]
        else:
            n = sum(m["role"] == "user" for m in body["messages"])
            chunks += [{"delta": {"content": "answer "}}, {"delta": {"content": str(n)}},
                       {"delta": {}, "finish_reason": "stop"}]
        events = [{"model": self.server.model_path, "choices": [{"index": 0, **c}]} for c in chunks]
        events.append({"model": self.server.model_path, "choices": [],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 7}})
        if getattr(self.server, "cut_stream", False):  # the stream stops early: no finish reason, no usage
            events = events[:3]
        payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        data = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _apply_template(self, body: dict) -> None:
        """A ChatML-like template with a reasoning effort setting and <think> blocks."""
        effort = body.get("reasoning_effort", "xhigh")
        if effort not in ("low", "medium", "xhigh"):
            return self._send(500, {"error": {"code": 500, "message": f"Unexpected reasoning effort {effort}. "
                                              "Supported types are xhigh (default), medium, and low."}})
        out = [f"<|system|>effort {effort}\n"]
        for m in body["messages"]:
            if m["role"] == "assistant" and m.get("reasoning_content"):
                out.append(f"<|assistant|>\n<think>\n{m['reasoning_content']}\n</think>\n\n{m.get('content') or ''}")
            else:
                out.append(f"<|{m['role']}|>{m.get('content') or ''}\n")
        if body.get("add_generation_prompt", True):
            out.append("<|assistant|>\n<think>\n")
        self._send(200, {"prompt": "".join(out)})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body, dict(self.headers)))
        if getattr(self.server, "refuse_chat", False) and self.path.startswith("/v1/"):
            self.connection.close()  # the client sees a dropped connection
            return
        if self.path == "/apply-template" and self.server.llamacpp:
            return self._apply_template(body)
        if self.path == "/completion" and self.server.llamacpp:
            return self._send(200, {"content": " forced answer", "tokens_predicted": 3, "stop_type": "eos"})
        if self.path == "/tokenize" and (self.server.llamacpp or getattr(self.server, "router_models", None)):
            self._send(200, {"tokens": body["content"].split()})
        elif self.path == "/v1/chat/completions" and body.get("stream"):
            self._stream_chat(body)
        elif self.path == "/v1/chat/completions":
            if self.server.fail_with:
                return self._send(self.server.fail_with, {"error": "boom"})
            if self.server.truncate_reasoning:  # every reply runs out of tokens while still thinking
                last = body["messages"][-1]
                if last["role"] == "user" and "thinking budget" in (last.get("content") or ""):
                    message = {"role": "assistant", "content": "follow-up answer"}
                    finish = "stop"
                else:
                    message = {"role": "assistant", "content": "", "reasoning_content": "long thought"}
                    finish = "length"
                return self._send(200, {"model": self.server.model_path,
                                        "choices": [{"message": message, "finish_reason": finish}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 7}})
            if self.server.scripted:  # a grading model: canned replies in order
                message = {"role": "assistant", "content": self.server.scripted.pop(0)}
                return self._send(200, {"model": "grader", "choices": [{"message": message, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
            if getattr(self.server, "garble_after_tool", False) and any(m["role"] == "tool" for m in body["messages"]):
                return self._send(500, {"error": {"code": 500, "message": "The model produced output that does "
                                                  "not match the expected peg-native format"}})
            has_result = any(m["role"] == "tool" for m in body["messages"])
            message = {"role": "assistant", "content": "", "reasoning_content": "let me think about it"}
            finish = "stop"
            if body.get("tools") and not has_result:
                name = body["tools"][0]["function"]["name"]
                message["tool_calls"] = [{"id": "call_1", "type": "function",
                                          "function": {"name": name, "arguments": '{"order_id": "A-1"}'}}]
                finish = "tool_calls"
            else:
                message["content"] = f"answer {sum(m['role'] == 'user' for m in body['messages'])}"
            self._send(200, {"model": self.server.model_path,
                             "choices": [{"message": message, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 7}})
        elif self.path == "/v1/messages":
            has_result = any(isinstance(m["content"], list) and any(b.get("type") == "tool_result"
                                                                    for b in m["content"])
                             for m in body["messages"])
            blocks = [{"type": "thinking", "thinking": "let me think about it", "signature": ""}]
            stop = "end_turn"
            if body.get("tools") and not has_result:
                blocks.append({"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"],
                               "input": {"order_id": "A-1"}})
                stop = "tool_use"
            else:
                blocks.append({"type": "text", "text": f"answer {len(body['messages']) // 2 + 1}"})
            self._send(200, {"model": self.server.model_path, "content": blocks, "stop_reason": stop,
                             "usage": {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 7}})
        else:
            self._send(404, {"error": "not found"})


class FakeServer:
    def __init__(self, llamacpp: bool = True, model_path: str = MODEL_PATH):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.llamacpp = llamacpp
        self.httpd.model_path = model_path
        self.httpd.requests = []
        self.httpd.fail_with = None
        self.httpd.scripted = []
        self.httpd.truncate_reasoning = False
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def requests(self):
        return self.httpd.requests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
