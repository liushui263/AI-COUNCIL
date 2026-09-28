"""HTTP 路由（基于 http.server）+ SSE + 静态前端（规格第 7 节）。"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import debate as debate_mod
from . import providers, store
from .providers import ProviderError

log = logging.getLogger("ai-council.server")

INDEX_HTML = store.ROOT / "frontend" / "index.html"
MAX_BODY = 1024 * 1024  # 背景材料可能较长，放宽到 1MB
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


# ---------------------------------------------------------------- providers

@route("GET", r"/api/providers")
def list_providers(h, body):
    h.send_json(providers.public_list())


def _provider_or_404(pid: str) -> dict:
    try:
        return providers.get_provider(pid)
    except ProviderError:
        raise ApiError(404, f"未知的 provider：{pid}") from None


@route("PUT", r"/api/providers/(?P<pid>[a-z0-9_-]+)/key")
def put_key(h, body, pid):
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
    p = _provider_or_404(pid)
    store.set_key(p["env"], None)
    log.info("已删除 %s 的 key", pid)
    h.send_json({"ok": True, "key_configured": providers.key_configured(pid)})


@route("POST", r"/api/providers/(?P<pid>[a-z0-9_-]+)/models/refresh")
def refresh_models(h, body, pid):
    _provider_or_404(pid)
    try:
        entry = providers.refresh_models(pid)
    except ProviderError as e:
        raise ApiError(502, f"刷新模型列表失败：{e.msg}") from None
    h.send_json({"provider": pid, "models": entry["models"], "updated_at": entry["updated_at"],
                 "default_model": providers.default_model(pid)})


# ---------------------------------------------------------------- debates

@route("POST", r"/api/debates")
def create_debate(h, body):
    try:
        debate_id = manager.create(body or {})
    except debate_mod.ValidationError as e:
        raise ApiError(400, str(e)) from None
    h.send_json({"debate_id": debate_id}, status=201)


@route("GET", r"/api/debates")
def list_debates(h, body):
    h.send_json(store.list_debates())


def _load_or_404(debate_id: str) -> dict:
    run = manager.get_run(debate_id)
    if run and run.active:
        return run.debate
    d = store.load_debate(debate_id)
    if not d:
        raise ApiError(404, "找不到这场辩论")
    return d


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)")
def get_debate(h, body, did):
    d = _load_or_404(did)
    out = dict(d)
    out["active"] = manager.is_active(did)
    out["usage"] = store.usage_total(d)
    h.send_json(out)


@route("DELETE", r"/api/debates/(?P<did>[0-9a-fA-F-]+)")
def delete_debate(h, body, did):
    try:
        ok = manager.delete(did)
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    if not ok:
        raise ApiError(404, "找不到这场辩论")
    h.send_json({"ok": True})


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/cancel")
def cancel_debate(h, body, did):
    _load_or_404(did)
    if not manager.cancel(did):
        raise ApiError(409, "这场辩论已经结束，无需取消")
    h.send_json({"ok": True, "note": "已请求取消：当前轮完成后停止"})


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/synthesize")
def synthesize(h, body, did):
    _load_or_404(did)
    try:
        manager.synthesize(did)
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    except KeyError:
        raise ApiError(404, "找不到这场辩论") from None
    h.send_json({"ok": True}, status=202)


@route("POST", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/resume")
def resume_debate(h, body, did):
    """从历史重新开启：body {"retry_failed": bool, "extra_rounds": int}"""
    _load_or_404(did)
    try:
        manager.resume(did, body or {})
    except debate_mod.ValidationError as e:
        raise ApiError(409, str(e)) from None
    except KeyError:
        raise ApiError(404, "找不到这场辩论") from None
    h.send_json({"ok": True}, status=202)


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/events")
def debate_events(h, body, did):
    run = manager.get_run(did)
    if not run:
        d = store.load_debate(did)
        if not d:
            raise ApiError(404, "找不到这场辩论")
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
    lines.append(f"- 轮次：{'盲写首轮 + ' if cfg['blind_round'] else '公开首轮 + '}{cfg['debate_rounds']} 轮交叉辩论")
    lines.append(f"- 综合者：{cfg['synthesis']['provider']}/{cfg['synthesis']['model']}")
    lines.append(f"- 用量：{u['calls']} 次调用，输入 {u['prompt_tokens']} tokens，输出 {u['completion_tokens']} tokens")
    lines.append("")
    if (d.get("background") or "").strip():
        lines += ["## 背景材料", "", d["background"].strip(), ""]
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


@route("GET", r"/api/debates/(?P<did>[0-9a-fA-F-]+)/export\.md")
def export_md(h, body, did):
    d = _load_or_404(did)
    data = export_markdown(d).encode("utf-8")
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", d["topic"])[:40].strip() or "debate"
    fname = urllib.parse.quote(f"AI议事厅-{safe}.md")
    h.send_response(200)
    h.send_header("Content-Type", "text/markdown; charset=utf-8")
    h.send_header("Content-Disposition", f"attachment; filename=\"debate-{did[:8]}.md\"; filename*=UTF-8''{fname}")
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
    def send_json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
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

    def _dispatch(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        try:
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


def make_server(host: str = "127.0.0.1", port: int = 8000, extra_clients=()) -> Server:
    srv = Server((host, port), Handler)
    srv.extra_clients = frozenset(extra_clients)
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
