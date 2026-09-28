"""HTTP 路由（基于 http.server）+ SSE + 静态前端（规格第 7 节）。"""
from __future__ import annotations

import base64
import http.cookies
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth as auth_mod
from . import debate as debate_mod
from . import export as export_mod
from . import extract, providers, store
from .providers import ProviderError

log = logging.getLogger("ai-council.server")

INDEX_HTML = store.ROOT / "frontend" / "index.html"
MAX_BODY = 16 * 1024 * 1024  # 上传的文档以 base64 放在 JSON 里（单个文件上限 10MB）
SSE_KEEPALIVE = 15

manager = debate_mod.DebateManager()

_ROUTES = []


def route(method: str, pattern: str):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        _ROUTES.append((method, rx, fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ---------------------------------------------------------------- 账号（仅共享模式）

@route("GET", r"/api/me")
def me(h, body):
    h.send_json({"mode": "shared" if h.auth else "local", "user": h.user})


@route("POST", r"/api/login")
def login(h, body):
    if not h.auth:
        raise ApiError(404, "单机模式不需要登录")
    b = body or {}
    try:
        token, created = h.auth.login(str(b.get("access") or ""), str(b.get("name") or ""),
                                      str(b.get("password") or ""), h.real_ip())
    except auth_mod.AuthError as e:
        log.info("登录失败（%s）：%s", h.real_ip(), e.msg)
        raise ApiError(e.status, e.msg) from None
    user = h.auth.user_for(token)
    log.info("%s %s（%s）", "新用户注册" if created else "登录", user, h.real_ip())
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
    h.extra_headers = [("Set-Cookie", f"{auth_mod.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                                      f"Max-Age={auth_mod.SESSION_TTL}{secure}")]
    h.send_json({"ok": True, "user": user, "created": created})


@route("POST", r"/api/logout")
def logout(h, body):
    if h.auth:
        h.auth.logout(h.session_token())
    h.extra_headers = [("Set-Cookie", f"{auth_mod.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")]
    h.send_json({"ok": True})


# ---------------------------------------------------------------- providers

@route("GET", r"/api/providers")
def list_providers(h, body):
    items = providers.public_list()
    if h.auth:
        # 共享模式：不透露本机 .env 里配了哪些 key；是否已配置由各人浏览器自己判断
        for it in items:
            it["key_configured"] = False
    h.send_json(items)


def _no_server_keys(h):
    if h.auth:
        raise ApiError(403, "共享模式下 key 只保存在你自己的浏览器里，服务器不保存")


def _provider_or_404(pid: str) -> dict:
    try:
        return providers.get_provider(pid)
    except ProviderError:
        raise ApiError(404, f"未知的 provider：{pid}") from None


@route("PUT", r"/api/providers/(?P<pid>[a-z0-9_-]+)/key")
def put_key(h, body, pid):
    _no_server_keys(h)
    p = _provider_or_404(pid)
    key = str((body or {}).get("key") or "").strip()
    if not key:
        raise ApiError(400, "key 不能为空")
    try:
        store.set_key(p["env"], key)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    log.info("已保存 %s 的 key（%s）", pid, store.mask(key))
    # 保存后立刻刷新模型列表：既验证 key，也让默认模型跟上官方的最新模型
    out = {"ok": True, "key_configured": True}
    try:
        entry = providers.refresh_models(pid)
        out.update(models=len(entry["models"]), default_model=providers.default_model(pid))
    except ProviderError as e:
        out["refresh_error"] = e.msg
    h.send_json(out)


@route("DELETE", r"/api/providers/(?P<pid>[a-z0-9_-]+)/key")
def delete_key(h, body, pid):
    _no_server_keys(h)
    p = _provider_or_404(pid)
    store.set_key(p["env"], None)
    log.info("已删除 %s 的 key", pid)
    h.send_json({"ok": True, "key_configured": providers.key_configured(pid)})


@route("POST", r"/api/providers/(?P<pid>[a-z0-9_-]+)/models/refresh")
def refresh_models(h, body, pid):
    _provider_or_404(pid)
    try:
        entry = providers.refresh_models(pid, h.keys)
    except ProviderError as e:
        raise ApiError(502, f"刷新模型列表失败：{e.msg}") from None
    h.send_json({"provider": pid, "models": entry["models"], "updated_at": entry["updated_at"],
                 "default_model": providers.default_model(pid)})


@route("POST", r"/api/extract")
def extract_text(h, body):
    """上传文档提取文字：body {"name": 文件名, "data": base64}；返回 {name, text, chars, truncated}"""
    if not isinstance(body, dict) or not body.get("name") or not body.get("data"):
        raise ApiError(400, "缺少文件")
    name = str(body["name"])
    try:
        data = base64.b64decode(str(body["data"]), validate=False)
    except (ValueError, TypeError):
        raise ApiError(400, "文件数据格式不正确") from None
    try:
        text = extract.extract(name, data)
    except extract.ExtractError as e:
        raise ApiError(422, f"{name}：{e}") from None
    except Exception:  # 解析器对畸形文件的兜底
        log.exception("提取 %s 失败", name)
        raise ApiError(422, f"{name}：无法读取这个文件") from None
    h.send_json({"name": name, "text": text[:extract.TEXT_LIMIT], "chars": len(text),
                 "truncated": len(text) > extract.TEXT_LIMIT})


# ---------------------------------------------------------------- debates

@route("POST", r"/api/debates")
def create_debate(h, body):
    try:
        debate_id = manager.create(body or {}, owner=h.user, keys=h.keys)
    except debate_mod.ValidationError as e:
        raise ApiError(400, str(e)) from None
    h.send_json({"debate_id": debate_id}, status=201)


@route("GET", r"/api/debates")
def list_debates(h, body):
    h.send_json(store.list_debates(h.user if h.auth else None))


def _load_or_404(h, debate_id: str) -> dict:
    """共享模式下只能访问自己的辩论；别人的一律当作不存在。"""
    run = manager.get_run(debate_id)
    d = run.debate if run and run.active else store.load_debate(debate_id)
    if not d or (h.auth and d.get("owner") != h.user):
        raise ApiError(404, "找不到这场辩论")
    return d


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)")
def get_debate(h, body, did):
    d = _load_or_404(h, did)
    out = dict(d)
    out["active"] = manager.is_active(did)
    out["usage"] = store.usage_total(d)
    h.send_json(out)


@route("DELETE", r"/api/debates/(?P<did>[0-9a-fA-F-]+)")
def delete_debate(h, body, did):
    _load_or_404(h, did)
    try:
        ok = manager.delete(did)
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    if not ok:
        raise ApiError(404, "找不到这场辩论")
    h.send_json({"ok": True})


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/cancel")
def cancel_debate(h, body, did):
    _load_or_404(h, did)
    if not manager.cancel(did):
        raise ApiError(409, "这场辩论已经结束，无需取消")
    h.send_json({"ok": True, "note": "已请求取消：当前轮完成后停止"})


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/synthesize")
def synthesize(h, body, did):
    _load_or_404(h, did)
    try:
        manager.synthesize(did, h.keys)
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    except KeyError:
        raise ApiError(404, "找不到这场辩论") from None
    h.send_json({"ok": True}, status=202)


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/resume")
def resume_debate(h, body, did):
    """从历史重新开启：body {"retry_failed": bool, "extra_rounds": int}"""
    _load_or_404(h, did)
    try:
        manager.resume(did, body or {}, h.keys)
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    except KeyError:
        raise ApiError(404, "找不到这场辩论") from None
    h.send_json({"ok": True}, status=202)


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/events")
def debate_events(h, body, did):
    d = _load_or_404(h, did)
    run = manager.get_run(did)
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    h.close_connection = True

    def write(event, data, seq=None):
        payload = ""
        if seq is not None:
            payload += f"id: {seq}\n"
        payload += f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        h.wfile.write(payload.encode("utf-8"))
        h.wfile.flush()

    try:
        if not run:
            # 不在内存里的历史辩论（例如重启后）：直接告知最终状态
            write("debate_done", {"status": d.get("status"), "archived": True})
            return
        try:
            pos = int(h.headers.get("Last-Event-ID", "-1")) + 1
        except ValueError:
            pos = 0
        while True:
            with run.cond:
                if pos >= len(run.events) and run.active:
                    run.cond.wait(timeout=SSE_KEEPALIVE)
                batch = run.events[pos:]
                active = run.active
            if batch:
                for ev in batch:
                    write(ev["event"], ev["data"], ev["seq"])
                pos += len(batch)
            elif active:
                h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
            if not active and pos >= len(run.events):
                return
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return


def export_markdown(d: dict) -> str:
    cfg = d["config"]
    u = store.usage_total(d)
    lines = [f"# AI 议事厅：{d['topic']}", ""]
    lines.append(f"- 时间：{d.get('created_at', '')}")
    lines.append(f"- 状态：{d.get('status', '')}")
    lines.append("- 参会者：" + "；".join(
        f"{p['id']}（{p['model']}，{p['role'] or debate_mod.DEFAULT_ROLE}）" for p in cfg["participants"]))
    lines.append(f"- 轮次：{'盲写首轮 + ' if cfg['blind_round'] else '公开首轮 + '}{max(0, len(d.get('rounds', [])) - 1)} 轮交叉辩论")
    lines.append(f"- 综合者：{cfg['synthesis']['provider']}/{cfg['synthesis']['model']}")
    lines.append(f"- 用量：{u['calls']} 次调用，输入 {u['prompt_tokens']} tokens，输出 {u['completion_tokens']} tokens")
    lines.append("")
    if (d.get("background") or "").strip():
        lines += ["## 背景材料", "", d["background"].strip(), ""]
    if d.get("attachments"):
        lines += ["附件：" + "、".join(a["name"] for a in d["attachments"]), ""]
    syn = d.get("synthesis") or {}
    lines += ["## 综合报告", ""]
    if syn.get("status") == "ok":
        lines.append(f"> 由 {syn.get('by')} 综合")
        lines.append("")
        # 报告里的二级标题降一级，保持导出文档层级整齐
        lines.append(re.sub(r"(?m)^(#{2,5}) ", lambda m: "#" + m.group(1) + " ", syn["text"]))
    else:
        lines.append(f"（未生成{('：' + syn['error']) if syn.get('error') else ''}）")
    lines += ["", "## 辩论记录", ""]
    for rnd in d.get("rounds", []):
        extra = debate_mod.input_summary(d, rnd["index"])
        if extra:
            lines += [f"### 提问者补充（第 {rnd['index']} 轮之前）", "", extra, ""]
        lines += [f"### {debate_mod.round_title(rnd['index'], cfg['blind_round'])}", ""]
        for m in rnd["messages"]:
            lines.append(f"#### {m['participant']}（{m['model']}，{m.get('role') or debate_mod.DEFAULT_ROLE}）")
            lines.append("")
            if m.get("status") == "ok":
                lines.append(m.get("text", ""))
            else:
                lines.append(f"> 发言失败：{m.get('error') or m.get('status')}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/export\.(?P<ext>md|docx|html)")
def export_file(h, body, did, ext):
    """导出：md 原文；docx 为 Word；html 为打印页，打开后在浏览器里“另存为 PDF”。"""
    d = _load_or_404(h, did)
    md = export_markdown(d)
    title = f"AI 议事厅：{d['topic']}"
    if ext == "md":
        data, ctype = md.encode("utf-8"), "text/markdown; charset=utf-8"
    elif ext == "docx":
        data = export_mod.to_docx(md, title)
        ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        data, ctype = export_mod.to_html(md, title).encode("utf-8"), "text/html; charset=utf-8"
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    if ext != "html":
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", d["topic"])[:40].strip() or "debate"
        fname = urllib.parse.quote(f"AI议事厅-{safe}.{ext}")
        h.send_header("Content-Disposition", f"attachment; filename=\"debate-{did[:8]}.{ext}\"; filename*=UTF-8''{fname}")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "AICouncil/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s - %s", self.address_string(), fmt % args)

    # -- helpers
    auth = None          # 共享模式下是 auth_mod.Auth
    user = None          # 共享模式下当前登录的人
    keys = None          # 共享模式下本次请求带来的 key（dict），单机模式为 None（用 .env）
    extra_headers = ()

    def send_json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in self.extra_headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        """防 DNS rebinding：只接受本机地址访问，保护本地 key。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self) -> bool:
        """写操作若带 Origin，必须来自本机页面（防其他网站跨站调用）。"""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        host = urllib.parse.urlsplit(origin).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1")

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "请求体过大")
        if n <= 0:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "请求体不是合法的 JSON") from None

    def _client_ok(self) -> bool:
        """只接受本机连接；WSL 下另外放行 Windows 主机转发过来的连接（见 Server.extra_clients）。"""
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        try:
            if ipaddress.ip_address(ip).is_loopback:
                return True
        except ValueError:
            return False
        return ip in getattr(self.server, "extra_clients", ())

    # -- 共享模式
    def real_ip(self) -> str:
        """经 Cloudflare Tunnel 等本机转发进来时，用转发头里的真实来源 IP（用于登录限流）。"""
        ip = self.client_address[0]
        try:
            loop = ipaddress.ip_address(ip.removeprefix("::ffff:")).is_loopback
        except ValueError:
            loop = False
        if loop:
            fwd = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            if fwd.strip():
                return fwd.strip()
        return ip

    def session_token(self):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        m = c.get(auth_mod.COOKIE)
        return m.value if m else None

    def _request_keys(self) -> dict:
        """共享模式：key 由浏览器放在 X-AIC-Keys 头里（base64 的 JSON），只在内存里用，不落盘、不记日志。"""
        raw = self.headers.get("X-AIC-Keys")
        if not raw:
            return {}
        try:
            data = json.loads(base64.b64decode(raw).decode("utf-8"))
        except ValueError:
            raise ApiError(400, "X-AIC-Keys 格式不正确") from None
        if not isinstance(data, dict):
            raise ApiError(400, "X-AIC-Keys 格式不正确")
        out = {}
        for pid, key in data.items():
            if isinstance(pid, str) and isinstance(key, str) and key.strip() and len(key) < 1000:
                try:
                    providers.get_provider(pid)
                except ProviderError:
                    continue
                out[pid] = key.strip()
        return out

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # 非浏览器请求；仍需要登录，Cookie 是 SameSite=Strict
        return urllib.parse.urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _dispatch(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        self.auth = getattr(self.server, "auth", None)
        self.extra_headers, self.user, self.keys = [], None, None  # 同一连接可能处理多个请求，逐个重置
        try:
            if self.auth:
                # 共享模式：谁都能连上来，但除了首页和登录，都要先登录
                if method != "GET" and not self._same_origin():
                    raise ApiError(403, "拒绝跨站请求")
                self.user = self.auth.user_for(self.session_token())
                if not self.user and path not in ("/", "/index.html", "/favicon.ico", "/api/me", "/api/login", "/api/logout"):
                    raise ApiError(401, "请先登录")
                self.keys = self._request_keys()
            else:
                if not self._client_ok():
                    raise ApiError(403, "只允许本机访问")
                if not self._host_ok():
                    raise ApiError(403, "只允许通过 127.0.0.1 访问")
                if method != "GET" and not self._origin_ok():
                    raise ApiError(403, "拒绝跨站请求")
            if method == "GET" and path in ("/", "/index.html"):
                return self._serve_index()
            if method == "GET" and path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            for m, rx, fn in _ROUTES:
                mt = rx.match(path)
                if mt and m == method:
                    body = self._read_body() if method in ("POST", "PUT") else None
                    return fn(self, body, **mt.groupdict())
            if any(rx.match(path) for _, rx, _ in _ROUTES):
                raise ApiError(405, "不支持的请求方法")
            raise ApiError(404, "not found")
        except ApiError as e:
            self.send_json({"error": e.msg}, status=e.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("处理 %s %s 出错", method, path)
            try:
                self.send_json({"error": f"服务器内部错误：{type(e).__name__}"}, status=500)
            except Exception:  # noqa: BLE001
                pass

    def _serve_index(self):
        try:
            data = INDEX_HTML.read_bytes()
        except OSError:
            raise ApiError(500, "找不到 frontend/index.html") from None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程同时绑定同一端口（例如上次启动的实例还没退出），
    # 浏览器的请求可能被另一个进程接走，表现为 ERR_EMPTY_RESPONSE。Windows 改用独占绑定。
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        log.exception("处理来自 %s 的请求时出错", client_address)


def port_in_use(host: str, port: int) -> bool:
    """端口上是否已有程序在接受连接（上次启动的实例或其他程序）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def make_server(host: str = "127.0.0.1", port: int = 8000, extra_clients=(), access_password: str | None = None) -> Server:
    """access_password 不为空时进入共享模式（给朋友测试）：要登录，按人分开历史，key 由各人浏览器提供。"""
    srv = Server((host, port), Handler)
    srv.extra_clients = frozenset(extra_clients)
    srv.auth = auth_mod.Auth(access_password) if access_password else None
    return srv


def is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def wsl_windows_host_ip() -> str | None:
    """WSL2（NAT 模式）里 Windows 主机的地址 = 默认网关，从 /proc/net/route 读取。"""
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
    except (OSError, ValueError):
        pass
    return None
