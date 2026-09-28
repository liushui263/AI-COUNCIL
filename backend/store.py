"""本地存储：.env 读写、辩论 JSON 存档、模型列表缓存（data/config.json）。

只用标准库。所有写操作走同一把锁，并通过“写临时文件再替换”保证文件不会写一半。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEBATES_DIR = DATA_DIR / "debates"
CONFIG_PATH = DATA_DIR / "config.json"
ENV_PATH = ROOT / ".env"

_lock = threading.RLock()
_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _ensure_dirs() -> None:
    DEBATES_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def now_iso() -> str:
    # 规格示例带本地时区偏移（+08:00），这里用本机时区
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- .env

def _parse_env(text: str) -> dict:
    """手写 .env 解析：KEY=VALUE，支持 # 注释、export 前缀、成对引号。"""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def read_env() -> dict:
    with _lock:
        if not ENV_PATH.exists():
            return {}
        return _parse_env(ENV_PATH.read_text(encoding="utf-8"))


def get_key(env_name: str) -> str:
    """优先读 .env，其次读进程环境变量。未配置返回空串。"""
    val = read_env().get(env_name, "")
    if not val:
        val = os.environ.get(env_name, "")
    return val.strip()


def set_key(env_name: str, value: str | None) -> None:
    """写入或删除（value 为 None/空）一个 key，保留 .env 里其他行与注释。"""
    if not _ENV_NAME_RE.match(env_name):
        raise ValueError("非法的环境变量名")
    if value is not None:
        value = value.strip()
        if any(c in value for c in "\r\n"):
            raise ValueError("key 不能包含换行")
    with _lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        new_lines, found = [], False
        for line in lines:
            s = line.strip()
            if s.startswith("export "):
                s = s[7:].lstrip()
            if s.split("=", 1)[0].strip() == env_name and "=" in s:
                if value and not found:
                    new_lines.append(f"{env_name}={value}")
                found = True
                continue
            new_lines.append(line)
        if value and not found:
            new_lines.append(f"{env_name}={value}")
        _atomic_write(ENV_PATH, "\n".join(new_lines) + ("\n" if new_lines else ""))


def mask(key: str) -> str:
    """日志脱敏：只保留前 4 位。"""
    if not key:
        return ""
    return key[:4] + "…"


# ---------------------------------------------------------------- config.json

def read_config() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            return {}
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def write_config(cfg: dict) -> None:
    with _lock:
        _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def cache_models(provider_id: str, models: list) -> dict:
    with _lock:
        cfg = read_config()
        entry = {"models": models, "updated_at": now_iso()}
        cfg[provider_id] = entry
        write_config(cfg)
        return entry


def cached_models(provider_id: str) -> dict | None:
    return read_config().get(provider_id)


# ---------------------------------------------------------------- 辩论存档

def valid_id(debate_id: str) -> bool:
    return bool(_ID_RE.match(debate_id or ""))


def _debate_path(debate_id: str) -> Path:
    if not valid_id(debate_id):
        raise ValueError("非法的辩论 id")
    return DEBATES_DIR / f"{debate_id}.json"


def save_debate(debate: dict) -> None:
    with _lock:
        _ensure_dirs()
        _atomic_write(_debate_path(debate["id"]), json.dumps(debate, ensure_ascii=False, indent=2))


def load_debate(debate_id: str) -> dict | None:
    try:
        path = _debate_path(debate_id)
    except ValueError:
        return None
    with _lock:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def delete_debate(debate_id: str) -> bool:
    try:
        path = _debate_path(debate_id)
    except ValueError:
        return False
    with _lock:
        if path.exists():
            path.unlink()
            return True
        return False


def list_debates(owner: str | None = None) -> list:
    """历史列表（按创建时间倒序），只返回摘要。owner 给定时（共享模式）只列这个人的。"""
    with _lock:
        _ensure_dirs()
        items = []
        for p in DEBATES_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if owner is not None and d.get("owner") != owner:
                continue
            items.append({
                "id": d.get("id"),
                "created_at": d.get("created_at"),
                "topic": d.get("topic", ""),
                "participants": len(d.get("config", {}).get("participants", [])),
                "status": d.get("status"),
                "usage": usage_total(d),
            })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


def usage_total(debate: dict) -> dict:
    """汇总一场辩论的 token 用量（各轮发言 + 综合）。"""
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def add(u):
        if not u:
            return
        tot["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        tot["completion_tokens"] += int(u.get("completion_tokens") or 0)

    for rnd in debate.get("rounds", []):
        for m in rnd.get("messages", []):
            if m.get("status") in ("ok", "failed"):
                tot["calls"] += 1
            add(m.get("usage"))
    syn = debate.get("synthesis") or {}
    if syn.get("status") in ("ok", "failed"):
        tot["calls"] += 1
    add(syn.get("usage"))
    return tot


def mark_interrupted_on_startup() -> int:
    """后端重启时：把仍处于 running 的辩论标记为 interrupted（规格 11 节）。"""
    n = 0
    with _lock:
        _ensure_dirs()
        for p in DEBATES_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            changed = False
            if d.get("status") in ("running", "synthesizing"):
                d["status"] = "interrupted"
                changed = True
            for rnd in d.get("rounds", []):
                for m in rnd.get("messages", []):
                    if m.get("status") in ("pending", "running"):
                        m["status"] = "failed"
                        m["error"] = "服务重启，发言中断"
                        changed = True
            if changed:
                _atomic_write(p, json.dumps(d, ensure_ascii=False, indent=2))
                n += 1
    return n
