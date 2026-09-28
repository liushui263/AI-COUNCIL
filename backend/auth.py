"""共享模式（给朋友测试）的登录：一个大家共用的访问密码 + 每人自己的名字和密码。

- 名字第一次出现时自动注册；之后必须用同一个密码登录。
- 密码只存 PBKDF2 哈希（data/users.json）；会话令牌只在内存里，重启后需要重新登录。
- 按来源 IP 限制登录失败次数，防止猜密码。
- 这里不碰任何 API key：共享模式下 key 只存在各人自己的浏览器里，每次请求带上来。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from . import store

NAME_RX = re.compile(r"^[\w一-鿿.-]{1,20}$")
PBKDF2_ROUNDS = 200_000
SESSION_TTL = 30 * 24 * 3600
FAIL_WINDOW, FAIL_LIMIT = 600, 10   # 10 分钟内最多失败 10 次
COOKIE = "aic_session"


class AuthError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


class Auth:
    def __init__(self, access_password: str):
        if not access_password:
            raise ValueError("共享模式必须设置访问密码")
        self.access_password = access_password
        self._lock = threading.Lock()
        self._sessions: dict = {}   # token -> (user, 过期时间)
        self._fails: dict = {}      # ip -> [失败时间]

    @property
    def users_path(self):
        return store.DATA_DIR / "users.json"

    def _users(self) -> dict:
        try:
            return json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_users(self, users: dict) -> None:
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.users_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.users_path)

    def _check_rate(self, ip: str) -> None:
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
        self._fails[ip] = fails
        if len(fails) >= FAIL_LIMIT:
            wait = int(FAIL_WINDOW - (now - fails[0])) + 1
            raise AuthError(429, f"登录失败次数太多，请 {wait} 秒后再试")

    def _fail(self, ip: str, status: int, msg: str):
        self._fails.setdefault(ip, []).append(time.time())
        return AuthError(status, msg)

    def login(self, access: str, name: str, password: str, ip: str) -> tuple:
        """返回 (token, 是否新注册)。"""
        name = (name or "").strip()
        with self._lock:
            self._check_rate(ip)
            if not hmac.compare_digest((access or "").encode(), self.access_password.encode()):
                raise self._fail(ip, 401, "访问密码不对")
            if not NAME_RX.match(name):
                raise AuthError(400, "名字 1~20 个字，只能用中文、字母、数字、下划线、点和横线")
            if len(password or "") < 4:
                raise AuthError(400, "个人密码至少 4 位")
            users = self._users()
            key = name.lower()
            created = key not in users
            if created:
                salt = secrets.token_bytes(16)
                users[key] = {"name": name, "salt": salt.hex(), "hash": _hash(password, salt), "created_at": store.now_iso()}
                self._save_users(users)
            else:
                u = users[key]
                if not hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"]):
                    raise self._fail(ip, 401, "这个名字已经有人用了，密码不对")
            token = secrets.token_urlsafe(32)
            self._sessions[token] = (users[key]["name"], time.time() + SESSION_TTL)
            return token, created

    def user_for(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            item = self._sessions.get(token)
            if not item:
                return None
            if item[1] < time.time():
                self._sessions.pop(token, None)
                return None
            return item[0]

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)
