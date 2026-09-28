"""离线端到端测试：用本地假 provider 跑通辩论全流程（不访问外网、不花钱）。

运行：python3 -m unittest discover -s tests -v    （在 ai-council 目录下）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import debate, providers, server, store  # noqa: E402
from tests import mock_providers  # noqa: E402

KEY = mock_providers.VALID_KEY


class OfflineE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 所有数据写到临时目录
        cls.tmp = Path(tempfile.mkdtemp(prefix="ai-council-test-"))
        store.DATA_DIR = cls.tmp / "data"
        store.DEBATES_DIR = store.DATA_DIR / "debates"
        store.CONFIG_PATH = store.DATA_DIR / "config.json"
        store.ENV_PATH = cls.tmp / ".env"
        for k in [p["env"] for p in providers.PROVIDERS]:
            os.environ.pop(k, None)

        cls.mock, base = mock_providers.start()
        patch = {"groq": "/openai", "openrouter": "/openai", "pollinations": "/openai",
                 "deepseek": "/openai", "gemini": "/gemini", "anthropic": "/anthropic"}
        for p in providers.PROVIDERS:
            if p["id"] in patch:
                p["base_url"] = base + patch[p["id"]]
                p.pop("fallback_chat_url", None)
                p.pop("fallback_models_url", None)

        providers.BACKOFF = (0.1, 0.1, 0.1)  # 测试里不真等
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.mock.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jcall(self, method, path, body=None, headers=None):
        st, raw = self.call(method, path, body, headers)
        return st, json.loads(raw) if raw else None

    def events(self, debate_id):
        """读完整个 SSE 流，返回 [(event, data)]。"""
        out, ev = [], None
        with urllib.request.urlopen(self.base + f"/api/debates/{debate_id}/events", timeout=60) as r:
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    out.append((ev, json.loads(line[6:])))
        return out

    def set_keys(self, *pids):
        for pid in pids:
            st, _ = self.jcall("PUT", f"/api/providers/{pid}/key", {"key": KEY})
            self.assertEqual(st, 200)

    def assert_no_key_leak(self, raw: bytes):
        self.assertNotIn(KEY.encode(), raw)
        self.assertNotIn(b"SECRET", raw)

    # ---------------------------------------------------------------- tests
    def test_01_providers_and_keys(self):
        st, ps = self.jcall("GET", "/api/providers")
        self.assertEqual(st, 200)
        self.assertEqual(len(ps), 8)
        self.assertFalse(next(p for p in ps if p["id"] == "gemini")["key_configured"])
        self.set_keys("gemini", "groq", "anthropic", "openrouter", "pollinations")
        st, raw = self.call("GET", "/api/providers")
        self.assert_no_key_leak(raw)
        self.assertTrue(next(p for p in json.loads(raw) if p["id"] == "gemini")["key_configured"])
        self.assertIn(f"GEMINI_API_KEY={KEY}", store.ENV_PATH.read_text())
        # 删除再加回
        st, r = self.jcall("DELETE", "/api/providers/groq/key")
        self.assertFalse(r["key_configured"])
        self.assertNotIn("GROQ_API_KEY", store.ENV_PATH.read_text())
        self.set_keys("groq")

    def test_02_models_refresh(self):
        st, r = self.jcall("POST", "/api/providers/gemini/models/refresh")
        self.assertEqual(st, 200, r)
        self.assertEqual(r["models"], ["gemini-2.5-flash"])
        st, r = self.jcall("POST", "/api/providers/openrouter/models/refresh")
        self.assertEqual(r["models"][0], "aa:free")
        self.assertEqual(r["default_model"], "aa:free")  # 默认取第一个 :free
        st, r = self.jcall("POST", "/api/providers/anthropic/models/refresh")
        self.assertEqual(r["models"], ["claude-haiku-4-5"])
        cfg = json.loads(store.CONFIG_PATH.read_text())
        self.assertIn("gemini", cfg)
        st, r = self.jcall("POST", "/api/providers/xai/models/refresh")
        self.assertEqual(st, 502)  # 未配置 key

    def test_03_validation(self):
        st, r = self.jcall("POST", "/api/debates", {"topic": "", "participants": []})
        self.assertEqual(st, 400)
        self.assertIn("话题", r["error"])
        st, r = self.jcall("POST", "/api/debates", {"topic": "x", "participants": [{"provider": "gemini"}]})
        self.assertEqual(st, 400)
        self.assertIn("至少", r["error"])
        st, r = self.jcall("POST", "/api/debates", {"topic": "x", "participants": [
            {"provider": "gemini"}, {"provider": "xai"}]})
        self.assertEqual(st, 400)
        self.assertIn("xAI", r["error"])
        st, r = self.jcall("POST", "/api/debates", {"topic": "x", "debate_rounds": 9, "participants": [
            {"provider": "gemini"}, {"provider": "groq"}]})
        self.assertEqual(st, 400)

    def test_04_full_debate(self):
        mock_providers.MockHandler.requests.clear()
        body = {
            "topic": "两款冰柜选哪个？", "background": "A 价格 3000，B 价格 2500",
            "participants": [
                {"provider": "gemini", "model": "gemini-2.5-flash", "role": "成本派"},
                {"provider": "groq", "model": "llama-x", "role": ""},
                {"provider": "anthropic", "model": "claude-haiku-4-5", "role": "风险派"},
                {"provider": "groq", "model": "bad-model", "role": "捣乱者"},  # 模拟失败
            ],
            "blind_round": True, "debate_rounds": 2,
        }
        st, r = self.jcall("POST", "/api/debates", body)
        self.assertEqual(st, 201, r)
        did = r["debate_id"]
        evs = self.events(did)
        names = [e for e, _ in evs]
        self.assertEqual(names[0], "debate_started")
        self.assertEqual(names[-1], "debate_done")
        self.assertEqual(evs[-1][1]["status"], "done")
        self.assertIn("token", names)
        self.assertEqual(names.count("round_done"), 3)
        fails = [d for e, d in evs if e == "message_failed"]
        self.assertEqual(len(fails), 3)  # 捣乱者每轮失败
        self.assertIn("does not exist", fails[0]["error"])
        self.assertEqual(fails[0]["participant"], "groq#2")

        d = store.load_debate(did)
        self.assertEqual(d["status"], "done")
        self.assertEqual(len(d["rounds"]), 3)
        for h in ("## 共识", "## 未解决的分歧", "## 各方保留意见", "## 最终建议"):
            self.assertIn(h, d["synthesis"]["text"])
        self.assertEqual(d["synthesis"]["by"], "gemini/gemini-2.5-flash")  # 默认=第一个参会者
        self.assertGreater(d["usage"]["prompt_tokens"], 0)

        reqs = mock_providers.MockHandler.requests
        def user_text(rq):
            b = rq["body"]
            if "messages" in b:
                return b["messages"][-1]["content"]
            return b["contents"][0]["parts"][0]["text"]
        # 盲写轮：互不引用（user 里没有别人的发言）
        blind = [rq for rq in reqs if "请给出你的分析与建议方案" in user_text(rq)]
        self.assertEqual(len(blind), 4)
        for rq in blind:
            self.assertNotIn("我的观点", user_text(rq))
            self.assertIn("背景信息：A 价格 3000", user_text(rq))
        # 交叉轮：只带其他人上一轮，不带自己
        cross = [rq for rq in reqs if "以下是其他 AI 在上一轮的观点" in user_text(rq)]
        self.assertEqual(len(cross), 8)
        for rq in cross:
            ut = user_text(rq)
            model = rq["body"].get("model") or rq["path"].split("/models/")[1].split(":")[0]
            self.assertNotIn(f"[{model}] 我的观点", ut)
        synth = [rq for rq in reqs if "综合报告" in user_text(rq)]
        self.assertEqual(len(synth), 1)
        self.assertIn("缺席", user_text(synth[0]))

        # 历史、详情、导出
        st, lst = self.jcall("GET", "/api/debates")
        self.assertTrue(any(x["id"] == did for x in lst))
        st, raw = self.call("GET", f"/api/debates/{did}")
        self.assert_no_key_leak(raw)
        st, raw = self.call("GET", f"/api/debates/{did}/export.md")
        md = raw.decode()
        self.assertEqual(st, 200)
        self.assertIn("### 共识", md)
        self.assertIn("第 2 轮：交叉辩论", md)
        self.assertIn("发言失败", md)
        # 重新综合
        st, r = self.jcall("POST", f"/api/debates/{did}/synthesize")
        self.assertEqual(st, 202, r)
        evs = self.events(did)
        self.assertIn("synthesis_done", [e for e, _ in evs])
        OfflineE2E.done_id = did

    def test_05_open_first_round(self):
        body = {"topic": "公开首轮", "blind_round": False, "debate_rounds": 0, "participants": [
            {"provider": "groq", "model": "m-a"}, {"provider": "openrouter", "model": "m-b"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        evs = self.events(r["debate_id"])
        self.assertEqual(evs[-1][1]["status"], "done")
        last = mock_providers.MockHandler.requests[-2]["body"]["messages"][-1]["content"]
        self.assertIn("[m-a] 我的观点", last)  # 第二位能看到第一位

    def test_06_cancel_and_manual_synthesis(self):
        body = {"topic": "取消测试", "debate_rounds": 3, "participants": [
            {"provider": "groq", "model": "slow"}, {"provider": "gemini", "model": "slow"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        did = r["debate_id"]
        time.sleep(0.5)
        st, c = self.jcall("POST", f"/api/debates/{did}/cancel")
        self.assertEqual(st, 200, c)
        evs = self.events(did)
        self.assertEqual(evs[-1][0], "debate_cancelled")
        d = store.load_debate(did)
        self.assertEqual(d["status"], "cancelled")
        self.assertEqual(len(d["rounds"]), 1)
        self.assertTrue(all(m["status"] == "ok" for m in d["rounds"][0]["messages"]))
        self.assertIsNone(d["synthesis"])
        st, r = self.jcall("POST", f"/api/debates/{did}/synthesize")
        self.assertEqual(st, 202)
        self.events(did)
        d = store.load_debate(did)
        self.assertEqual(d["synthesis"]["status"], "ok")
        self.assertEqual(d["status"], "cancelled")

    def test_07_rate_limit_retry_then_fail(self):
        with self.assertRaises(providers.ProviderError) as cm:
            providers.chat("groq", "always-429", "s", "u", 200, 0.5, False)
        self.assertIn("已重试 2 次", cm.exception.msg)  # 默认重试 2 次
        with self.assertRaises(providers.ProviderError) as cm:
            providers.chat("groq", "always-429", "s", "u", 200, 0.5, False, max_retries=0)
        self.assertIn("已重试 0 次", cm.exception.msg)
        notes = []
        r = providers.chat("groq", "fail-once-a", "s", "u", 200, 0.5, False, on_retry=lambda n, w: notes.append(n))
        self.assertIn("我的观点", r["text"])
        self.assertIn("503", notes[0])

    def test_08_bad_key(self):
        store.set_key("DEEPSEEK_API_KEY", "wrong-key-XYZ")
        with self.assertRaises(providers.ProviderError) as cm:
            providers.chat("deepseek", "deepseek-chat", "s", "u", 200, 0.5, True)
        self.assertIn("key 无效", cm.exception.msg)
        self.assertNotIn("wrong-key-XYZ", cm.exception.msg)

    def test_09_all_fail_round(self):
        body = {"topic": "全失败", "participants": [
            {"provider": "groq", "model": "bad-model"}, {"provider": "gemini", "model": "bad-model"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        evs = self.events(r["debate_id"])
        self.assertEqual(evs[-1][1]["status"], "partial")
        d = store.load_debate(r["debate_id"])
        self.assertEqual(len(d["rounds"]), 1)

    def test_10_restart_marks_interrupted(self):
        fake = {"id": "11111111-2222-3333-4444-555555555555", "created_at": store.now_iso(), "topic": "t",
                "background": "", "config": {"participants": []}, "status": "running",
                "rounds": [{"index": 0, "messages": [{"participant": "g", "status": "running"}]}], "synthesis": None}
        store.save_debate(fake)
        self.assertGreaterEqual(store.mark_interrupted_on_startup(), 1)
        d = store.load_debate(fake["id"])
        self.assertEqual(d["status"], "interrupted")
        # 不在内存里的辩论，SSE 直接告知状态
        evs = self.events(fake["id"])
        self.assertEqual(evs, [("debate_done", {"status": "interrupted", "archived": True})])
        st, _ = self.jcall("DELETE", f"/api/debates/{fake['id']}")
        self.assertEqual(st, 200)
        st, _ = self.jcall("GET", f"/api/debates/{fake['id']}")
        self.assertEqual(st, 404)

    def test_11_security(self):
        st, r = self.jcall("GET", "/api/providers", headers={"Host": "evil.example.com"})
        self.assertEqual(st, 403)
        st, r = self.jcall("PUT", "/api/providers/gemini/key", {"key": "x"}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(st, 403)
        st, raw = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("AI 议事厅".encode(), raw)
        self.assertNotIn(b"http://cdn", raw)
        self.assertNotIn(b"https://cdn", raw)

    def test_13_default_model_follows_list(self):
        pick = providers.pick_default
        self.assertEqual(pick("gemini", ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite",
                                         "gemini-3.8-flash-image", "text-embedding-004", "gemini-3.9-flash-preview"]),
                         "gemini-3.8-flash")
        self.assertEqual(pick("groq", ["whisper-large-v3", "meta-llama/llama-guard-4-12b", "qwen/qwen3-32b"]),
                         "qwen/qwen3-32b")
        self.assertIsNone(pick("openrouter", ["a/b"]))  # 没有免费模型时不替用户选付费的
        # 保存 key 会自动刷新模型列表并返回新默认模型
        st, r = self.jcall("PUT", "/api/providers/anthropic/key", {"key": KEY})
        self.assertEqual(r["models"], 1)
        self.assertEqual(r["default_model"], "claude-haiku-4-5")
        st, r = self.jcall("PUT", "/api/providers/xai/key", {"key": "bad"})
        self.assertIn("refresh_error", r)
        store.set_key("XAI_API_KEY", None)

    def test_14_same_provider_serial_and_synth_fallback(self):
        mock_providers.MockHandler.spans.clear()
        body = {"topic": "串行与综合兜底", "debate_rounds": 0,
                "participants": [{"provider": "groq", "model": "slow"}, {"provider": "groq", "model": "slow"},
                                 {"provider": "gemini", "model": "g-ok"}],
                "synthesis": {"provider": "anthropic", "model": "always-503"}}
        st, r = self.jcall("POST", "/api/debates", body)
        evs = self.events(r["debate_id"])
        spans = sorted(s for s in mock_providers.MockHandler.spans if s[0] == "slow")
        self.assertEqual(len(spans), 3)  # 2 次发言 + 1 次兜底综合
        self.assertLessEqual(spans[0][2], spans[1][1] + 0.05)  # 同一 provider 不重叠
        d = store.load_debate(r["debate_id"])
        self.assertEqual(d["status"], "done")
        self.assertEqual(d["synthesis"]["by"], "groq/slow")
        self.assertIn("always-503", d["synthesis"]["fallback_from"][0])
        self.assertTrue(any(e == "synthesis_retry" and "改由" in x.get("note", "") for e, x in evs))

    def test_15_resume_retry_failed_and_extend(self):
        body = {"topic": "从历史继续", "debate_rounds": 1, "participants": [
            {"provider": "groq", "model": "ok-1"}, {"provider": "gemini", "model": "bad-model"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        did = r["debate_id"]
        self.events(did)
        d = store.load_debate(did)
        self.assertEqual([m["status"] for m in d["rounds"][0]["messages"]], ["ok", "failed"])
        # 把失败的一方换成能用的模型（模拟用户换模型或过一阵额度恢复），然后重试失败的发言并追加 1 轮
        for rnd in d["rounds"]:
            rnd["messages"][1]["model"] = "g-fixed"
        d["config"]["participants"][1]["model"] = "g-fixed"
        store.save_debate(d)
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {"retry_failed": True, "extra_rounds": 1})
        self.assertEqual(st, 202, r)
        evs = self.events(did)
        self.assertEqual(evs[-1], ("debate_done", {"status": "done"}))
        d = store.load_debate(did)
        self.assertEqual(len(d["rounds"]), 3)
        self.assertEqual(d["config"]["debate_rounds"], 2)
        self.assertTrue(all(m["status"] == "ok" for rnd in d["rounds"] for m in rnd["messages"]))
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {"extra_rounds": 99})
        self.assertEqual(st, 409)

    def test_17_continue_with_input_and_model_change(self):
        mock_providers.MockHandler.requests.clear()
        body = {"topic": "对话式续辩", "debate_rounds": 0, "participants": [
            {"provider": "groq", "model": "old-a"}, {"provider": "gemini", "model": "old-b"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        did = r["debate_id"]
        self.events(did)
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {
            "extra_rounds": 2, "user_input": "预算只有 2800，怎么选？",
            "participants": [{"id": "groq", "model": "new-a"}],
            "synthesis": {"provider": "gemini", "model": "new-s"}})
        self.assertEqual(st, 202, r)
        self.events(did)
        d = store.load_debate(did)
        self.assertEqual(len(d["rounds"]), 3)
        self.assertEqual(d["rounds"][1]["messages"][0]["model"], "new-a")
        self.assertEqual(d["inputs"][0]["before_round"], 1)
        self.assertEqual(len(d["syntheses"]), 2)
        self.assertEqual(d["synthesis"]["by"], "gemini/new-s")
        reqs = mock_providers.MockHandler.requests
        def ut(rq):
            b = rq["body"]
            return b["messages"][-1]["content"] if "messages" in b else b["contents"][0]["parts"][0]["text"]
        r1 = [rq for rq in reqs if "预算只有 2800" in ut(rq) and "以下是其他 AI" in ut(rq)]
        self.assertEqual(len(r1), 2)  # 第 1 轮两位都收到了补充
        self.assertIn("上一次综合报告", ut(r1[0]))
        synth = [rq for rq in reqs if "综合报告（Markdown）" in ut(rq)][-1]
        self.assertIn("提问者在第 1 轮之前补充", ut(synth))
        # 总轮数不设上限：再追加 10 轮以上也允许（分多次）
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {"extra_rounds": 11})
        self.assertEqual(st, 409)
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {"user_input": "x"})
        self.assertEqual(st, 409)

    def test_18_upload_and_attachments(self):
        import base64, io, zipfile, zlib
        def up(name, data):
            return self.jcall("POST", "/api/extract", {"name": name, "data": base64.b64encode(data).decode()})
        st, r = up("a.md", "# 冰柜对比\n价格 2800".encode())
        self.assertEqual(st, 200, r)
        self.assertIn("价格 2800", r["text"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                       '<w:body><w:p><w:r><w:t>docx 里的预算表</w:t></w:r></w:p></w:body></w:document>')
        st, r = up("b.docx", buf.getvalue())
        self.assertEqual((st, r["text"]), (200, "docx 里的预算表"))
        c = zlib.compress(b"BT /F1 12 Tf 72 700 Td (PDF text) Tj ET")
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
               b"4 0 obj<</Type/Font/Subtype/Type1>>endobj\n5 0 obj<</Filter/FlateDecode>>stream\n" + c + b"\nendstream endobj\n")
        st, r = up("c.pdf", pdf)
        self.assertEqual((st, r["text"]), (200, "PDF text"))
        st, r = up("d.xlsx", b"x")
        self.assertEqual(st, 422)
        st, r = up("e.txt", ("长" * 30000).encode())
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["text"]), 20000)

        mock_providers.MockHandler.requests.clear()
        body = {"topic": "带附件", "debate_rounds": 0, "background": "手写背景",
                "attachments": [{"name": "价格表.pdf", "text": "附件里的价格 1999"}],
                "participants": [{"provider": "groq", "model": "m-a"}, {"provider": "gemini", "model": "m-b"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        did = r["debate_id"]
        self.events(did)
        st, r = self.jcall("POST", f"/api/debates/{did}/resume", {
            "extra_rounds": 1, "attachments": [{"name": "补充.docx", "text": "补充文档内容 42"}]})
        self.assertEqual(st, 202, r)
        self.events(did)
        d = store.load_debate(did)
        self.assertEqual(d["attachments"][0]["name"], "价格表.pdf")
        self.assertEqual(d["inputs"][0]["attachments"][0]["name"], "补充.docx")
        texts = [json.dumps(rq["body"], ensure_ascii=False) for rq in mock_providers.MockHandler.requests]
        self.assertEqual(sum("附件里的价格 1999" in t and "手写背景" in t for t in texts), 2)  # 首轮两位都收到
        self.assertEqual(sum("补充文档内容 42" in t for t in texts), 2)
        synth = [t for t in texts if "综合报告（Markdown）" in t][-1]
        self.assertIn("附件：补充.docx", synth)
        self.assertNotIn("补充文档内容 42", synth)

    def test_19_retry_event_has_countdown(self):
        body = {"topic": "倒计时", "debate_rounds": 0, "participants": [
            {"provider": "groq", "model": "fail-once-cd"}, {"provider": "gemini", "model": "ok"}]}
        st, r = self.jcall("POST", "/api/debates", body)
        evs = self.events(r["debate_id"])
        rt = [x for e, x in evs if e == "participant_retry"]
        self.assertTrue(rt)
        self.assertGreater(rt[0]["retry_at"], 1e12)

    def test_16_quote_trim(self):
        u = debate.cross_user("t", [{"participant": "a", "model": "m", "role": "", "text": "字" * 5000}])
        self.assertLess(len(u), 3400)
        self.assertIn("后文略", u)

    def test_12_prompts(self):
        self.assertNotIn("背景信息", debate.blind_user("t", "  "))
        self.assertIn("中立客观的专家", debate.BLIND_SYSTEM.format(role=debate.DEFAULT_ROLE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
