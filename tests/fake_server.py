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
        if self.path == "/props" and self.server.llamacpp:
            self._send(200, {"model_path": MODEL_PATH, "model_ftype": "Q8_0", "build_info": "b11023-4ff829ec2",
                             "default_generation_settings": {"n_ctx": 4096, "params": {"temperature": 0.7}}})
        elif self.path == "/v1/models":
            model_id = MODEL_PATH if self.server.llamacpp else "served-model-name"
            self._send(200, {"data": [{"id": model_id, "meta": {"n_params": 27000000000}}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body, dict(self.headers)))
        if self.path == "/tokenize" and self.server.llamacpp:
            self._send(200, {"tokens": body["content"].split()})
        elif self.path == "/v1/chat/completions":
            if self.server.fail_with:
                return self._send(self.server.fail_with, {"error": "boom"})
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
            self._send(200, {"model": MODEL_PATH, "choices": [{"message": message, "finish_reason": finish}],
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
            self._send(200, {"model": MODEL_PATH, "content": blocks, "stop_reason": stop,
                             "usage": {"input_tokens": 4, "cache_read_input_tokens": 6, "output_tokens": 7}})
        else:
            self._send(404, {"error": "not found"})


class FakeServer:
    def __init__(self, llamacpp: bool = True):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.llamacpp = llamacpp
        self.httpd.requests = []
        self.httpd.fail_with = None
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
