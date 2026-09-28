"""离线测试用的假 provider 服务：模拟 OpenAI-compatible / Gemini / Anthropic 三类接口。

约定（通过模型名控制行为）：
  bad-model    → 400 模型不存在
  always-429   → 一直 429
  slow         → 每个 token 间隔 0.2s（用于测试取消）
  always-503   → 一直 503 过载
  fail-once*   → 同名模型第一次调用返回 503（带 Retry-After: 0），之后正常
其余模型正常返回；system 里含“综合者”时返回带四个固定章节的综合报告。
收到的请求都记录在 MockHandler.requests 里，便于断言（如盲写轮互不引用）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_KEY = "test-key-SECRET-1234567890"

SYNTH_TEXT = (
    "## 共识\n各方都认为要看能耗。\n\n## 未解决的分歧\n- 价格：mock-a 与 mock-b 冲突。\n\n"
    "## 各方保留意见\n无。\n\n## 最终建议\n选 A，前提是预算充足。"
)


def _answer(system: str, user: str, model: str) -> str:
    if "综合者" in system:
        return SYNTH_TEXT
    return f"[{model}] 我的观点：选 A。理由一二三。"


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []
    calls: dict = {}
    spans: list = []  # (model, start, end)，用于检查同一 provider 是否串行
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _json(self, status, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, obj, event=None):
        s = (f"event: {event}\n" if event else "") + f"data: {json.dumps(obj) if not isinstance(obj, str) else obj}\n\n"
        self.wfile.write(s.encode())
        self.wfile.flush()

    def _key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        if self.headers.get("x-api-key"):
            return self.headers["x-api-key"]
        if "key=" in self.path:
            return self.path.split("key=", 1)[1].split("&", 1)[0]
        return ""

    def do_GET(self):
        if self._key() != VALID_KEY:
            return self._json(401, {"error": {"message": "invalid key"}})
        if self.path.startswith("/gemini/models"):
            return self._json(200, {"models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]}]})
        if self.path.startswith("/anthropic/models"):
            return self._json(200, {"data": [{"id": "claude-haiku-4-5"}], "has_more": False})
        if self.path.startswith("/openai/models"):
            return self._json(200, {"data": [{"id": "m1"}, {"id": "zz:free"}, {"id": "aa:free"}]})
        self._json(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with self.lock:
            MockHandler.requests.append({"path": self.path.split("?")[0], "body": body})
        if self._key() != VALID_KEY:
            return self._json(401, {"error": {"message": "invalid key"}})
        if self.path.startswith("/openai/"):
            t0 = time.time()
            self._openai(body)
            with self.lock:
                MockHandler.spans.append((body.get("model"), t0, time.time()))
            return
        if self.path.startswith("/gemini/"):
            return self._gemini(body)
        if self.path.startswith("/anthropic/"):
            return self._anthropic(body)
        self._json(404, {"error": "nope"})

    def _common(self, model):
        with self.lock:
            n = MockHandler.calls[model] = MockHandler.calls.get(model, 0) + 1
        if model.startswith("fail-once") and n == 1:
            self._json(503, {"error": {"message": "overloaded"}}, {"Retry-After": "0"})
            return True
        if model == "always-503":
            self._json(503, {"error": {"message": "This model is currently experiencing high demand."}})
            return True
        if model == "bad-model":
            self._json(400, {"error": {"message": f"model {model} does not exist"}})
            return True
        if model == "always-429":
            self._json(429, {"error": {"message": "rate limited"}})
            return True
        return False

    def _chunks(self, text, model):
        step = 6
        for i in range(0, len(text), step):
            if model == "slow":
                time.sleep(0.2)
            yield text[i:i + step]

    def _openai(self, body):
        model = body["model"]
        if self._common(model):
            return
        msgs = body["messages"]
        text = _answer(msgs[0]["content"], msgs[1]["content"], model)
        usage = {"prompt_tokens": 11, "completion_tokens": 22}
        if not body.get("stream"):
            return self._json(200, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": usage})
        self._sse_start()
        for c in self._chunks(text, model):
            self._sse({"choices": [{"delta": {"content": c}}]})
        self._sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self._sse({"choices": [], "usage": usage})
        self._sse("[DONE]")

    def _gemini(self, body):
        model = self.path.split("/models/", 1)[1].split(":", 1)[0]
        if self._common(model):
            return
        system = body["systemInstruction"]["parts"][0]["text"]
        user = body["contents"][0]["parts"][0]["text"]
        text = _answer(system, user, model)
        usage = {"promptTokenCount": 5, "candidatesTokenCount": 7}
        if ":generateContent" in self.path:
            return self._json(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                                    "usageMetadata": usage})
        self._sse_start()
        chunks = list(self._chunks(text, model))
        for i, c in enumerate(chunks):
            obj = {"candidates": [{"content": {"parts": [{"text": c}]}}]}
            if i == len(chunks) - 1:
                obj["candidates"][0]["finishReason"] = "STOP"
                obj["usageMetadata"] = usage
            self._sse(obj)

    def _anthropic(self, body):
        model = body["model"]
        if self._common(model):
            return
        text = _answer(body["system"], body["messages"][0]["content"], model)
        if not body.get("stream"):
            return self._json(200, {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                    "usage": {"input_tokens": 3, "output_tokens": 4}})
        self._sse_start()
        self._sse({"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 1}}}, "message_start")
        for c in self._chunks(text, model):
            self._sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": c}}, "content_block_delta")
        self._sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}, "message_delta")
        self._sse({"type": "message_stop"}, "message_stop")


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
