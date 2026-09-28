"""各家 API 适配器：统一 chat() 接口 + 模型列表刷新（规格第 5 节）。

三类接入方式：
  A. OpenAI-compatible   POST {base}/chat/completions
  B. Gemini native       POST .../models/{model}:generateContent
  C. Anthropic native    POST https://api.anthropic.com/v1/messages

只用标准库。所有请求：连接超时 10s、读取超时 120s；429 指数退避（1s、4s）最多重试 2 次；
超时/网络错误重试 1 次；401/403 不重试。统一抛 ProviderError(msg, retryable)。
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store

log = logging.getLogger("ai-council.providers")

CONNECT_TIMEOUT = 10
BACKOFF = (2, 8, 20)  # 429/5xx 没有等待提示时的退避秒数；长度即最多重试次数
MAX_WAIT = 65         # 单次最多等多久；超过（如每日额度用完）就不等了
READ_TIMEOUT = 120
APP_NAME = "AI Council"
APP_URL = "http://127.0.0.1:8000"


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = False, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(msg)
        self.msg = msg
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after  # 服务端建议的等待秒数（Retry-After 头或错误文本里的提示）

    def __str__(self) -> str:
        return self.msg


# ---------------------------------------------------------------- Provider 清单（5.3）
# default_model 只是初始值；模型名在前端可编辑、可从 /models 刷新，不写死。

PROVIDERS = [
    {
        "id": "gemini", "name": "Gemini", "kind": "gemini", "env": "GEMINI_API_KEY",
        "need_key": True,
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        # 用官方的 latest 别名，模型换代时自动跟随；刷新列表后会按规则重选
        "default_model": "gemini-flash-latest",
        "free": True,
        "free_tier_desc": "免费档：Flash 系列免费，无需绑卡；RPM/RPD 按模型不同（以 AI Studio 显示为准）",
        "key_url": "https://aistudio.google.com/app/apikey",
    },
    {
        "id": "groq", "name": "Groq", "kind": "openai", "env": "GROQ_API_KEY",
        "need_key": True,
        "base_url": "https://api.groq.com/openai/v1",
        # TODO(规格待确认)：Groq 模型换得很快，保存 key 后会自动刷新列表并重选
        "default_model": "openai/gpt-oss-120b",
        "free": True,
        "free_tier_desc": "免费档：约 30 RPM + 每日 token 上限（分模型），无需绑卡",
        "key_url": "https://console.groq.com/keys",
    },
    {
        "id": "openrouter", "name": "OpenRouter", "kind": "openai", "env": "OPENROUTER_API_KEY",
        "need_key": True,
        "base_url": "https://openrouter.ai/api/v1",
        # 规格：动态取 /models 里第一个 :free 模型；保存 key 后会自动刷新列表
        # TODO(规格待确认)：刷新前的占位值，指望 OpenRouter 的免费路由，不可用时刷新列表即可
        "default_model": "openrouter/free",
        "free": True,
        "free_tier_desc": "免费：模型名加 :free 后缀；未充值账户 50 次/天、20 次/分钟；一次性充值 $10 起永久解锁 1000 次/天",
        "key_url": "https://openrouter.ai/keys",
    },
    {
        "id": "pollinations", "name": "Pollinations", "kind": "openai", "env": "POLLINATIONS_API_KEY",
        # 2026-09 起新端点要求 key（报 401 A valid API key is required），key 在 enter.pollinations.ai 免费领
        "need_key": True,
        "base_url": "https://gen.pollinations.ai/v1",
        # 旧端点保留为备用：新端点失败（非鉴权错误）时自动切换
        "fallback_chat_url": "https://text.pollinations.ai/openai",
        "fallback_models_url": "https://text.pollinations.ai/models",
        "default_model": "openai",
        "free": True,
        "free_tier_desc": "免费；需要在 enter.pollinations.ai 免费领 key",
        "key_url": "https://enter.pollinations.ai",
    },
    {
        "id": "deepseek", "name": "DeepSeek", "kind": "openai", "env": "DEEPSEEK_API_KEY",
        "need_key": True,
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "free": False,
        "free_tier_desc": "按量付费，极便宜",
        "key_url": "https://platform.deepseek.com",
    },
    {
        "id": "openai", "name": "OpenAI", "kind": "openai", "env": "OPENAI_API_KEY",
        "need_key": True,
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "free": False,
        "free_tier_desc": "按量付费",
        "key_url": "https://platform.openai.com",
    },
    {
        "id": "anthropic", "name": "Anthropic", "kind": "anthropic", "env": "ANTHROPIC_API_KEY",
        "need_key": True,
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-haiku-4-5",
        "free": False,
        "free_tier_desc": "按量付费；注意：Claude Pro/Claude Code 订阅不含 API 额度，必须单独开 API 账单",
        "key_url": "https://console.anthropic.com",
    },
    {
        "id": "xai", "name": "xAI", "kind": "openai", "env": "XAI_API_KEY",
        "need_key": True,
        "base_url": "https://api.x.ai/v1",
        # TODO(规格待确认)：规格写“以官方为准”，先用便宜的 mini 模型，可在页面改或刷新列表
        "default_model": "grok-3-mini",
        "free": False,
        "free_tier_desc": "按量付费",
        "key_url": "https://console.x.ai",
    },
]

_BY_ID = {p["id"]: p for p in PROVIDERS}


def get_provider(provider_id: str) -> dict:
    p = _BY_ID.get(provider_id)
    if not p:
        raise ProviderError(f"未知的 provider：{provider_id}")
    return p


def key_for(provider_id: str) -> str:
    return store.get_key(get_provider(provider_id)["env"])


def key_configured(provider_id: str) -> bool:
    return bool(key_for(provider_id))


def key_ok(provider_id: str) -> bool:
    """需要 key 的 provider 必须已配置 key；不需要的总是 OK。"""
    p = get_provider(provider_id)
    return (not p["need_key"]) or key_configured(provider_id)


# 模型会频繁上下线，内置默认值很快过期；刷新过模型列表后按下面的规则从列表里挑一个。
# 排除非对话模型（语音、图像、嵌入、审核等）
_NON_CHAT = ("embed", "whisper", "tts", "audio", "image", "imagen", "veo", "vision-preview", "guard", "moderation",
             "transcribe", "realtime", "search", "dall-e", "sora", "live", "robotics", "computer-use", "aqa",
             "learnlm", "playai", "orpheus", "prompt-guard", "safeguard", "codex", "instruct")
# 各家偏好：按顺序匹配关键字，命中的第一组里取“版本号最大”的那个
_PREFER = {
    "gemini": [("flash",), ("pro",)],
    "groq": [("gpt-oss-120b",), ("llama", "70b"), ("gpt-oss",), ("llama",), ("qwen",), ("kimi",)],
    "deepseek": [("deepseek-chat",), ("chat",)],
    "openai": [("gpt", "mini"), ("gpt",)],
    "anthropic": [("haiku",), ("sonnet",)],
    "xai": [("grok", "mini"), ("grok", "fast"), ("grok",)],
    "pollinations": [("openai",), ("mistral",)],
}
_AVOID = ("lite", "nano", "preview", "exp", "thinking", "latest-", "-8b", "tuning", "deep-research")


def _version_key(name: str):
    return [int(x) for x in re.findall(r"\d+", name)]


def pick_default(provider_id: str, models: list) -> str | None:
    """从模型列表里挑一个适合对话的默认模型；挑不出返回 None。"""
    chat = [m for m in models if not any(bad in m.lower() for bad in _NON_CHAT)]
    if not chat:
        return None
    if provider_id == "openrouter":
        free = [m for m in chat if m.endswith(":free")]
        return free[0] if free else None  # 没有免费模型就不替用户选付费的
    for keys in _PREFER.get(provider_id, []):
        hits = [m for m in chat if all(k in m.lower() for k in keys)]
        good = [m for m in hits if not any(a in m.lower() for a in _AVOID)] or hits
        if good:
            return max(good, key=_version_key)
    return chat[0]


def default_model(provider_id: str) -> str:
    """优先用刷新得到的模型列表：内置默认值还在列表里就用它，否则按规则从列表里挑。"""
    p = get_provider(provider_id)
    cached = store.cached_models(provider_id) or {}
    models = cached.get("models") or []
    if provider_id == "openrouter" and models:
        return pick_default(provider_id, models) or p["default_model"]
    if not models or p["default_model"] in models:
        return p["default_model"]
    return pick_default(provider_id, models) or p["default_model"]


def public_list() -> list:
    """GET /api/providers 的返回：绝不包含 key 明文。"""
    out = []
    for p in PROVIDERS:
        cached = store.cached_models(p["id"]) or {}
        out.append({
            "id": p["id"],
            "name": p["name"],
            "need_key": p["need_key"],
            "key_configured": key_configured(p["id"]),
            "free": p["free"],
            "free_tier_desc": p["free_tier_desc"],
            "key_url": p["key_url"],
            "default_model": default_model(p["id"]),
            "models": cached.get("models") or [],
            "models_updated_at": cached.get("updated_at"),
        })
    return out


# ---------------------------------------------------------------- HTTP 底层

class _TimeoutHTTPSConnection(http.client.HTTPSConnection):
    """连接阶段用 10s 超时，连上之后读取用 120s 超时（urllib 只支持单一超时）。"""

    def connect(self):
        self.timeout = CONNECT_TIMEOUT
        super().connect()
        self.sock.settimeout(READ_TIMEOUT)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_TimeoutHTTPSConnection, req, context=self._context)


# 保留系统代理设置（ProxyHandler 默认读 HTTPS_PROXY 等环境变量）
_opener = urllib.request.build_opener(_HTTPSHandler(context=ssl.create_default_context()))


def _scrub(text: str, key: str) -> str:
    if key and text:
        text = text.replace(key, store.mask(key))
    return text


def _error_message(status: int, body: bytes) -> str:
    """从 provider 的错误响应里取出可读信息。"""
    raw = body.decode("utf-8", "replace").strip()
    msg = ""
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            err = data.get("error", data)
            if isinstance(err, dict):
                msg = err.get("message") or err.get("msg") or ""
                meta = err.get("metadata") or {}
                if isinstance(meta, dict) and meta.get("raw"):
                    msg = f"{msg}（{str(meta['raw'])[:300]}）"
            elif isinstance(err, str):
                msg = err
            if not msg:
                msg = data.get("message") or data.get("detail") or ""
    except ValueError:
        msg = raw[:300]
    return msg or raw[:300] or f"HTTP {status}"


def _retry_after(headers, text: str) -> float | None:
    """服务端建议的等待时间：Retry-After 头，或错误文本里的 “try again in 32.4s” / “retryDelay: 30s”。"""
    try:
        v = headers.get("Retry-After") if headers else None
        if v:
            return float(v)
    except (TypeError, ValueError):
        pass
    text = text or ""
    m = re.search(r"(?:try again|retry) in\s+((?:[\d.]+\s*(?:ms|h|m|s|min|seconds?)\s*)+)", text, re.I) \
        or re.search(r"retryDelay[\"':\s]+([\d.]+s)", text)
    if not m:
        return None
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)\s*(ms|h|min|m|s|seconds?)", m.group(1).rstrip(".")):
        try:
            n = float(num.rstrip("."))
        except ValueError:
            continue
        unit = unit.lower()
        total += n / 1000 if unit == "ms" else n * 3600 if unit == "h" else n * 60 if unit in ("m", "min") else n
    return total or None


def _classify_http_error(status: int, body: bytes, key: str) -> ProviderError:
    detail = _scrub(_error_message(status, body), key)
    if status == 429:
        return ProviderError(f"429 限流：{detail}", retryable=True, status=status)
    if status in (401, 403):
        return ProviderError(f"{status} key 无效或额度不足，请检查设置页（{detail}）", retryable=False, status=status)
    if status == 402:
        return ProviderError(f"402 余额不足：{detail}", retryable=False, status=status)
    if status == 404:
        return ProviderError(f"404 {detail}（模型可能已下线或无权使用：到设置页点「刷新模型列表」，或换一个模型）",
                             retryable=False, status=status)
    if status >= 500:
        return ProviderError(f"{status} 服务端错误：{detail}", retryable=True, status=status)
    return ProviderError(f"{status} {detail}", retryable=False, status=status)


def _open(url: str, headers: dict, body: dict | None, key: str, method: str | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": "ai-council/1.0", "Accept": "application/json, text/event-stream"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        return _opener.open(req, timeout=READ_TIMEOUT)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()
        except Exception:  # noqa: BLE001
            err_body = b""
        err = _classify_http_error(e.code, err_body, key)
        err.retry_after = _retry_after(e.headers, err.msg)
        raise err from None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise ProviderError(f"网络错误：{_scrub(str(reason), key)}", retryable=True) from None
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        raise ProviderError(f"网络错误或超时：{_scrub(str(e) or type(e).__name__, key)}", retryable=True) from None


def _read_json(resp, key: str) -> dict:
    try:
        with resp:
            raw = resp.read()
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        raise ProviderError(f"读取响应超时或中断：{_scrub(str(e) or type(e).__name__, key)}", retryable=True) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ProviderError(f"无法解析的响应：{_scrub(raw[:200].decode('utf-8', 'replace'), key)}") from None


def _iter_sse(resp, key: str):
    """解析 SSE：逐个产出 (event, data_str)。"""
    event, data_lines = None, []
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        yield event, "\n".join(data_lines)
                    event, data_lines = None, []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data_lines.append(value)
            if data_lines:
                yield event, "\n".join(data_lines)
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        raise ProviderError(f"流式读取超时或中断：{_scrub(str(e) or type(e).__name__, key)}", retryable=True) from None


# ---------------------------------------------------------------- A. OpenAI-compatible

def _openai_headers(p: dict, key: str) -> dict:
    h = {}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if p["id"] == "openrouter":
        h["HTTP-Referer"] = APP_URL
        h["X-Title"] = APP_NAME
    return h


def _openai_chat_once(p, url, key, body, stream, on_token):
    resp = _open(url, _openai_headers(p, key), body, key)
    if not stream:
        data = _read_json(resp, key)
        if isinstance(data.get("error"), (dict, str)):
            raise ProviderError(_scrub(_error_message(200, json.dumps(data).encode()), key))
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("响应中没有 choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):  # 少数兼容实现返回分段内容
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        fin = choices[0].get("finish_reason")
        if fin == "length":
            text += "\n\n[因 max_tokens 上限被截断]"
        return {"text": text, "usage": _openai_usage(data.get("usage"))}

    parts, usage, fin = [], None, None
    for _event, data_str in _iter_sse(resp, key):
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except ValueError:
            continue
        if isinstance(chunk.get("error"), (dict, str)):
            raise ProviderError(_scrub(_error_message(200, data_str.encode()), key), retryable=False)
        if chunk.get("usage"):
            usage = _openai_usage(chunk["usage"])
        for ch in chunk.get("choices") or []:
            delta = (ch.get("delta") or {}).get("content")
            if delta:
                parts.append(delta)
                if on_token:
                    on_token(delta)
            if ch.get("finish_reason"):
                fin = ch["finish_reason"]
    text = "".join(parts)
    if fin == "length":
        tail = "\n\n[因 max_tokens 上限被截断]"
        text += tail
        if on_token:
            on_token(tail)
    return {"text": text, "usage": usage}


def _openai_usage(u):
    if not isinstance(u, dict):
        return None
    return {"prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0)}


def _chat_openai(p, model, system, user, max_tokens, temperature, stream, on_token, key):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": bool(stream),
    }
    if stream and p["id"] in ("openai", "groq", "deepseek", "openrouter", "xai"):
        body["stream_options"] = {"include_usage": True}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    try:
        return _openai_chat_once(p, url, key, body, stream, on_token)
    except ProviderError as e:
        # 新一代 OpenAI 推理模型不接受 max_tokens / temperature：按错误提示自动调整一次
        low = e.msg.lower()
        if e.status == 400 and ("max_completion_tokens" in low or "temperature" in low):
            if "max_completion_tokens" in low:
                body["max_completion_tokens"] = body.pop("max_tokens")
            if "temperature" in low:
                body.pop("temperature", None)
            return _openai_chat_once(p, url, key, body, stream, on_token)
        # Pollinations：新端点非鉴权失败时切到旧端点
        if p.get("fallback_chat_url") and e.status not in (401, 403, 429):
            log.warning("%s 新端点失败（%s），切换到备用端点", p["id"], e.msg)
            return _openai_chat_once(p, p["fallback_chat_url"], key, body, stream, on_token)
        raise


# ---------------------------------------------------------------- B. Gemini native

def _gemini_extract(chunk: dict):
    text, fin = "", None
    for cand in (chunk.get("candidates") or [])[:1]:
        for part in ((cand.get("content") or {}).get("parts") or []):
            if part.get("text") and not part.get("thought"):
                text += part["text"]
        fin = cand.get("finishReason") or fin
    return text, fin


def _gemini_usage(chunk: dict):
    u = chunk.get("usageMetadata")
    if not isinstance(u, dict):
        return None
    return {"prompt_tokens": int(u.get("promptTokenCount") or 0),
            "completion_tokens": int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)}


def _chat_gemini(p, model, system, user, max_tokens, temperature, stream, on_token, key):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    m = urllib.parse.quote(model.removeprefix("models/"), safe="-._:")
    base = p["base_url"].rstrip("/")
    qk = urllib.parse.quote(key, safe="")
    if not stream:
        url = f"{base}/models/{m}:generateContent?key={qk}"
        data = _read_json(_open(url, {}, body, key), key)
        if not data.get("candidates"):
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(f"Gemini 未返回内容{('：' + block) if block else ''}")
        text, fin = _gemini_extract(data)
        usage = _gemini_usage(data)
    else:
        url = f"{base}/models/{m}:streamGenerateContent?alt=sse&key={qk}"
        parts, fin, usage = [], None, None
        for _event, data_str in _iter_sse(_open(url, {}, body, key), key):
            try:
                chunk = json.loads(data_str)
            except ValueError:
                continue
            if isinstance(chunk.get("error"), dict):
                raise ProviderError(_scrub(_error_message(200, data_str.encode()), key))
            t, f = _gemini_extract(chunk)
            if t:
                parts.append(t)
                if on_token:
                    on_token(t)
            fin = f or fin
            usage = _gemini_usage(chunk) or usage
        text = "".join(parts)
    if fin and fin != "STOP":
        tail = f"\n\n[生成未正常结束：finishReason={fin}]"
        text += tail
        if stream and on_token:
            on_token(tail)
    return {"text": text, "usage": usage}


# ---------------------------------------------------------------- C. Anthropic native

def _anthropic_headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}


def _chat_anthropic(p, model, system, user, max_tokens, temperature, stream, on_token, key):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        # Anthropic 的 temperature 范围是 0~1，规格允许 0~2，这里截断
        "temperature": max(0.0, min(1.0, float(temperature))),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    url = p["base_url"].rstrip("/") + "/messages"
    if not stream:
        data = _read_json(_open(url, _anthropic_headers(key), body, key), key)
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        u = data.get("usage") or {}
        if data.get("stop_reason") == "max_tokens":
            text += "\n\n[因 max_tokens 上限被截断]"
        return {"text": text, "usage": {"prompt_tokens": int(u.get("input_tokens") or 0),
                                         "completion_tokens": int(u.get("output_tokens") or 0)}}

    body["stream"] = True
    parts, pin, pout, stop = [], 0, 0, None
    for event, data_str in _iter_sse(_open(url, _anthropic_headers(key), body, key), key):
        try:
            d = json.loads(data_str)
        except ValueError:
            continue
        et = d.get("type") or event
        if et == "message_start":
            u = (d.get("message") or {}).get("usage") or {}
            pin = int(u.get("input_tokens") or 0)
            pout = int(u.get("output_tokens") or 0)
        elif et == "content_block_delta":
            delta = d.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                parts.append(delta["text"])
                if on_token:
                    on_token(delta["text"])
        elif et == "message_delta":
            u = d.get("usage") or {}
            pout = int(u.get("output_tokens") or pout)
            stop = (d.get("delta") or {}).get("stop_reason") or stop
        elif et == "error":
            err = d.get("error") or {}
            retry = err.get("type") in ("overloaded_error", "rate_limit_error", "api_error")
            raise ProviderError(_scrub(f"Anthropic 错误：{err.get('message') or data_str[:200]}", key), retryable=retry)
    text = "".join(parts)
    if stop == "max_tokens":
        tail = "\n\n[因 max_tokens 上限被截断]"
        text += tail
        if on_token:
            on_token(tail)
    return {"text": text, "usage": {"prompt_tokens": pin, "completion_tokens": pout}}


_CHAT_BY_KIND = {"openai": _chat_openai, "gemini": _chat_gemini, "anthropic": _chat_anthropic}


# ---------------------------------------------------------------- 统一入口 + 重试

def chat(provider_id: str, model: str, system: str, user: str,
         max_tokens: int, temperature: float, stream: bool,
         on_token=None, on_retry=None) -> dict:
    """返回 {"text": str, "usage": {"prompt_tokens": int, "completion_tokens": int} | None}

    on_retry(msg)：发生重试前回调（流式输出已发出的内容需要前端清空重来）。
    """
    p = get_provider(provider_id)
    key = key_for(provider_id)
    if p["need_key"] and not key:
        raise ProviderError(f"{p['name']} 未配置 key，请到设置页配置")
    model = (model or "").strip() or default_model(provider_id)
    fn = _CHAT_BY_KIND[p["kind"]]

    # TODO(规格待确认)：规格写 429 退避 1s、4s 最多 2 次。免费档实测经常要等 30s 左右（Groq 按分钟算 token），
    # 所以改为：优先按服务端提示的时间等；没提示就 2s、8s、20s；503 过载同样处理。单次最多等 MAX_WAIT 秒。
    busy_retries, net_retries = 0, 0
    while True:
        try:
            return fn(p, model, system, user, int(max_tokens), float(temperature), stream, on_token, key)
        except ProviderError as e:
            busy = e.status == 429 or (e.status is not None and e.status >= 500)
            if busy and busy_retries < len(BACKOFF):
                wait = e.retry_after if e.retry_after is not None else BACKOFF[busy_retries]
                if wait > MAX_WAIT:
                    # 例如每日额度用完，要等几小时：不空等，直接失败
                    e.msg = f"{e.msg}（需要等待约 {int(wait)} 秒，超过上限，放弃重试）"
                    raise
                wait = max(wait, 0.5)
                busy_retries += 1
                what = "限流" if e.status == 429 else "服务繁忙"
                log.info("%s/%s %s %s，%.0fs 后第 %s 次重试", provider_id, model, e.status, what, wait, busy_retries)
                if on_retry:
                    on_retry(f"{e.status} {what}，{wait:.0f} 秒后重试（第 {busy_retries} 次）")
                time.sleep(wait)
                continue
            if e.status is None and e.retryable and net_retries < 1:
                net_retries += 1
                log.info("%s/%s 网络错误，重试一次：%s", provider_id, model, e.msg)
                if on_retry:
                    on_retry("网络错误或超时，重试 1 次")
                time.sleep(1)
                continue
            if busy:
                e.msg = f"{e.msg}（已重试 {busy_retries} 次）"
            raise


# ---------------------------------------------------------------- 模型列表刷新

def list_models(provider_id: str) -> list:
    p = get_provider(provider_id)
    key = key_for(provider_id)
    if p["need_key"] and not key:
        raise ProviderError(f"{p['name']} 未配置 key，无法获取模型列表")
    base = p["base_url"].rstrip("/")
    models: list = []
    if p["kind"] == "gemini":
        token = ""
        for _ in range(10):  # 分页
            url = f"{base}/models?pageSize=1000&key={urllib.parse.quote(key, safe='')}"
            if token:
                url += f"&pageToken={urllib.parse.quote(token)}"
            data = _read_json(_open(url, {}, None, key), key)
            for m in data.get("models") or []:
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    models.append(m.get("name", "").removeprefix("models/"))
            token = data.get("nextPageToken") or ""
            if not token:
                break
    elif p["kind"] == "anthropic":
        after = ""
        for _ in range(10):
            url = f"{base}/models?limit=1000" + (f"&after_id={urllib.parse.quote(after)}" if after else "")
            data = _read_json(_open(url, _anthropic_headers(key), None, key), key)
            models += [m["id"] for m in data.get("data") or [] if m.get("id")]
            if not data.get("has_more"):
                break
            after = data.get("last_id") or ""
    else:
        try:
            data = _read_json(_open(base + "/models", _openai_headers(p, key), None, key), key)
        except ProviderError as e:
            if p.get("fallback_models_url") and e.status not in (401, 403):
                data = _read_json(_open(p["fallback_models_url"], _openai_headers(p, key), None, key), key)
            else:
                raise
        items = data.get("data") if isinstance(data, dict) else data
        for m in items or []:
            if isinstance(m, str):
                models.append(m)
            elif isinstance(m, dict):
                mid = m.get("id") or m.get("name")
                if mid:
                    models.append(mid)
    # 去重保序
    seen, out = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    if provider_id == "openrouter":
        # 免费模型排在前面，方便选
        out.sort(key=lambda x: (not x.endswith(":free"), x))
    return out


def refresh_models(provider_id: str) -> dict:
    models = list_models(provider_id)
    return store.cache_models(provider_id, models)


def refresh_configured_in_background(max_age_hours: float = 24) -> None:
    """启动时：为已配置 key、模型缓存缺失或过期的 provider 在后台刷新模型列表。"""
    import threading
    from datetime import datetime

    def stale(pid):
        entry = store.cached_models(pid) or {}
        try:
            age = datetime.now().astimezone() - datetime.fromisoformat(entry["updated_at"])
            return age.total_seconds() > max_age_hours * 3600
        except (KeyError, TypeError, ValueError):
            return True

    def work():
        for p in PROVIDERS:
            if key_configured(p["id"]) and stale(p["id"]):
                try:
                    n = len(refresh_models(p["id"])["models"])
                    log.info("已刷新 %s 的模型列表（%d 个），默认模型：%s", p["name"], n, default_model(p["id"]))
                except ProviderError as e:
                    log.warning("刷新 %s 的模型列表失败：%s", p["name"], e.msg)

    threading.Thread(target=work, daemon=True, name="refresh-models").start()
