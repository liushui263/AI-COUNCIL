"""辩论引擎：参数校验、轮次编排、并发调用、综合、事件推送（规格第 6 节）。

每场辩论在后台线程里跑；过程中的每个事件追加到内存事件列表，SSE 连接从列表里读取
（断线重连可从头回放）。各轮结果随时写入 data/debates/{id}.json。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import extract, providers, store
from .providers import ProviderError

log = logging.getLogger("ai-council.debate")

MIN_PARTICIPANTS, MAX_PARTICIPANTS = 2, 8
MAX_DEBATE_ROUNDS = 5
QUOTE_LIMIT = 3000      # 交叉轮引用他人发言时每段最多字符数，控制 token（免费档常按分钟限 token）
REPORT_LIMIT = 2000     # 续辩时附带的上一次综合报告最多字符数
MAX_EXTRA_ROUNDS = 10
MAX_RETRIES = 5         # 每次发言最多重试次数的上限（默认见 providers.DEFAULT_RETRIES）   # 单次续辩最多追加的轮数；总轮数不设上限
DEFAULT_ROLE = "中立客观的专家"
TERMINAL_EVENTS = ("debate_done", "debate_cancelled")

# ---------------------------------------------------------------- 提示词模板（6.1）

BLIND_SYSTEM = (
    "你是一位{role}。请针对以下主题给出你的独立分析和具体建议方案。\n"
    "要求：结构清晰，给出明确结论和理由；不要提及其他 AI（你现在看不到它们的观点）。"
)
# 盲写关闭时的首轮：按顺序发言，后发言者能看到先发言者的观点
# TODO(规格待确认)：规格只说“盲写首轮”可关，没说关掉后首轮怎么跑；这里按“依次公开发言”实现
OPEN_SYSTEM = (
    "你是一位{role}。请针对以下主题给出你的分析和具体建议方案。\n"
    "要求：结构清晰，给出明确结论和理由；如果已有其他 AI 发言，可以参考并回应它们的观点。"
)
SYNTH_SYSTEM = "你是一位中立的综合者，只基于给定的辩论记录做综合，不引入外部知识，不虚构。"


def _speaker_label(msg: dict) -> str:
    role = msg.get("role") or DEFAULT_ROLE
    return f"{msg['participant']}（{msg['model']}，{role}）"


def blind_user(topic: str, background: str) -> str:
    lines = [f"主题：{topic}"]
    if background.strip():
        lines.append(f"背景信息：{background}")
    lines.append("请给出你的分析与建议方案。")
    return "\n".join(lines)


def open_user(topic: str, background: str, earlier: list) -> str:
    lines = [f"主题：{topic}"]
    if background.strip():
        lines.append(f"背景信息：{background}")
    if earlier:
        lines.append("\n以下是已经发言的其他 AI 的观点：\n")
        for m in earlier:
            lines.append(f"【{_speaker_label(m)}】\n{m['text']}\n")
    lines.append("请给出你的分析与建议方案。")
    return "\n".join(lines)


def cross_user(topic: str, others: list, user_input: str = "", last_report: str = "") -> str:
    parts = [f"主题：{topic}", ""]
    if user_input.strip():
        parts += ["提问者在这一轮之前补充了新的内容或问题：", user_input.strip(), ""]
        if last_report.strip():
            rep = last_report.strip()
            if len(rep) > REPORT_LIMIT:
                rep = rep[:REPORT_LIMIT] + "……（后文略）"
            parts += ["上一次综合报告（供参考）：", rep, ""]
    parts += ["以下是其他 AI 在上一轮的观点：", ""]
    if others:
        for m in others:
            text = m["text"]
            if len(text) > QUOTE_LIMIT:
                text = text[:QUOTE_LIMIT] + "……（篇幅所限，后文略）"
            parts += ["", f"【{_speaker_label(m)}】", text, ""]
    else:
        parts += ["（其他参会者上一轮均未能发言）", ""]
    parts += [
        "请针对以上观点：",
        "1) 指出你认同的部分及原因；",
        "2) 指出你反对或认为有漏洞的部分及原因；",
        "3) 给出修正后的最终建议。如无修正必要，说明原因并维持原方案。",
    ]
    if user_input.strip():
        parts.append("4) 务必正面回应提问者的新补充。")
    return "\n".join(parts)


def _retry_at(wait: float) -> int:
    """重试时刻（毫秒时间戳），前端据此显示倒计时。"""
    return int((time.time() + wait) * 1000)


def attachments_of(raw) -> list:
    """规范化上传的附件：[{name, text}]，每个截断到 TEXT_LIMIT 字。"""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValidationError("attachments 必须是数组")
    if len(raw) > extract.MAX_FILES:
        raise ValidationError(f"最多上传 {extract.MAX_FILES} 个文件")
    out = []
    for a in raw:
        if not isinstance(a, dict) or not str(a.get("text") or "").strip():
            raise ValidationError("附件格式不正确")
        out.append({"name": str(a.get("name") or "附件")[:200],
                    "text": str(a["text"]).strip()[:extract.TEXT_LIMIT]})
    return out


def with_attachments(text: str, atts: list) -> str:
    parts = [text.strip()] if text and text.strip() else []
    for a in atts or []:
        parts.append(f"【附件：{a['name']}】\n{a['text']}")
    return "\n\n".join(parts)


def full_background(debate: dict) -> str:
    return with_attachments(debate.get("background") or "", debate.get("attachments"))


def _input_at(debate: dict, idx: int):
    for it in debate.get("inputs") or []:
        if it.get("before_round") == idx:
            return it
    return None


def input_before(debate: dict, idx: int) -> str:
    """提问者在第 idx 轮之前的补充（含附件全文），用于发言提示。"""
    it = _input_at(debate, idx)
    return with_attachments(it.get("text") or "", it.get("attachments")) if it else ""


def input_summary(debate: dict, idx: int) -> str:
    """同上，但附件只列文件名（综合报告只需要知道补充了什么）。"""
    it = _input_at(debate, idx)
    if not it:
        return ""
    names = [a["name"] for a in it.get("attachments") or []]
    text = it.get("text") or ""
    return (text + (f"\n（附件：{'、'.join(names)}）" if names else "")).strip()


def build_transcript(debate: dict) -> str:
    """综合用的完整记录。格式：按轮分节，每条发言以【参会者（模型，角色）】开头。"""
    out = []
    participants = debate["config"]["participants"]
    ever_ok = set()
    for rnd in debate["rounds"]:
        extra = input_summary(debate, rnd["index"])
        if extra:
            out.append(f"=== 提问者在第 {rnd['index']} 轮之前补充 ===\n{extra}\n")
        title = round_title(rnd["index"], debate["config"]["blind_round"])
        out.append(f"=== {title} ===\n")
        for m in rnd["messages"]:
            if m["status"] == "ok":
                ever_ok.add(m["participant"])
                out.append(f"【{_speaker_label(m)}】\n{m['text']}\n")
            else:
                out.append(f"【{_speaker_label(m)}】（本轮未能发言：{m.get('error') or m['status']}）\n")
    absent = [p["id"] for p in participants if p["id"] not in ever_ok]
    if absent:
        out.append(f"注：以下参会者全程未能发言（缺席）：{'、'.join(absent)}。请在报告中注明。\n")
    return "\n".join(out)


def synth_user(transcript: str) -> str:
    return (
        "以下是多位 AI 就同一主题辩论的完整记录：\n\n"
        f"{transcript}\n\n"
        "请输出一份综合报告（Markdown），必须包含以下四个二级标题：\n"
        "## 共识\n## 未解决的分歧\n## 各方保留意见\n## 最终建议\n"
        "要求：分歧点注明是哪几方的观点冲突；最终建议给出倾向性结论及前提条件。"
    )


def round_title(index: int, blind: bool) -> str:
    if index == 0:
        return "第 0 轮：盲写" if blind else "第 0 轮：公开首轮"
    return f"第 {index} 轮：交叉辩论"


# ---------------------------------------------------------------- 参数校验

class ValidationError(ValueError):
    pass


def _num(v, cast, name, lo, hi, default):
    if v is None or v == "":
        return default
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} 必须是数字") from None
    if not (lo <= x <= hi):
        raise ValidationError(f"{name} 超出范围（{lo}~{hi}）")
    return x


def validate(body: dict) -> dict:
    """校验 POST /api/debates 的输入，返回规范化后的配置；非法时抛 ValidationError（中文）。"""
    if not isinstance(body, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    topic = str(body.get("topic") or "").strip()
    if not topic:
        raise ValidationError("请填写辩论话题")
    background = str(body.get("background") or "")

    raw_ps = body.get("participants") or []
    if not isinstance(raw_ps, list):
        raise ValidationError("participants 必须是数组")
    if len(raw_ps) < MIN_PARTICIPANTS:
        raise ValidationError(f"至少需要 {MIN_PARTICIPANTS} 位参会者（同一个 provider 可以添加多次，例如换不同角色）")
    if len(raw_ps) > MAX_PARTICIPANTS:
        raise ValidationError(f"最多 {MAX_PARTICIPANTS} 位参会者")

    participants, used_ids, missing_keys = [], {}, []
    for rp in raw_ps:
        if not isinstance(rp, dict):
            raise ValidationError("参会者格式不正确")
        pid = str(rp.get("provider") or "")
        try:
            prov = providers.get_provider(pid)
        except ProviderError:
            raise ValidationError(f"未知的 provider：{pid}") from None
        if not providers.key_ok(pid) and prov["name"] not in missing_keys:
            missing_keys.append(prov["name"])
        model = str(rp.get("model") or "").strip() or providers.default_model(pid)
        role = str(rp.get("role") or "").strip()
        # 同一 provider 出现多次时用 gemini、gemini#2 区分
        used_ids[pid] = used_ids.get(pid, 0) + 1
        part_id = pid if used_ids[pid] == 1 else f"{pid}#{used_ids[pid]}"
        participants.append({"id": part_id, "provider": pid, "model": model, "role": role})
    if missing_keys:
        raise ValidationError(f"以下 provider 需要 key 但尚未配置：{'、'.join(missing_keys)}。请先到设置页配置")

    blind = body.get("blind_round", True)
    blind = blind if isinstance(blind, bool) else str(blind).lower() not in ("0", "false", "off", "no")
    rounds = _num(body.get("debate_rounds"), int, "交叉辩论轮数", 0, MAX_DEBATE_ROUNDS, 2)
    max_tokens = _num(body.get("max_tokens"), int, "max_tokens", 200, 8000, 1500)
    temperature = _num(body.get("temperature"), float, "temperature", 0.0, 2.0, 0.7)
    max_retries = _num(body.get("max_retries"), int, "重试次数", 0, MAX_RETRIES, providers.DEFAULT_RETRIES)

    syn = body.get("synthesis") or {}
    if not isinstance(syn, dict) or not syn.get("provider"):
        syn = {"provider": participants[0]["provider"], "model": participants[0]["model"]}
    spid = str(syn.get("provider"))
    try:
        sprov = providers.get_provider(spid)
    except ProviderError:
        raise ValidationError(f"未知的综合者 provider：{spid}") from None
    if not providers.key_ok(spid):
        raise ValidationError(f"综合者 {sprov['name']} 需要 key 但尚未配置，请先到设置页配置")
    smodel = str(syn.get("model") or "").strip() or providers.default_model(spid)

    return {
        "topic": topic,
        "background": background,
        "attachments": attachments_of(body.get("attachments")),
        "config": {
            "participants": participants,
            "blind_round": blind,
            "debate_rounds": rounds,
            "synthesis": {"provider": spid, "model": smodel},
            "max_tokens": max_tokens,
            "temperature": temperature,
            "max_retries": max_retries,
        },
    }


# ---------------------------------------------------------------- 运行中的辩论

class Run:
    def __init__(self, debate: dict):
        self.debate = debate
        self.events: list = []
        self.cond = threading.Condition()
        self.cancel_flag = threading.Event()
        self.active = True
        self.thread: threading.Thread | None = None

    def emit(self, event: str, data: dict) -> None:
        with self.cond:
            self.events.append({"seq": len(self.events), "event": event, "data": data})
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.active = False
            self.cond.notify_all()


class DebateManager:
    def __init__(self):
        self._runs: dict = {}
        self._lock = threading.Lock()

    # -------- 对外接口

    def create(self, body: dict) -> str:
        cfg = validate(body)
        debate_id = str(uuid.uuid4())
        debate = {
            "id": debate_id,
            "created_at": store.now_iso(),
            "topic": cfg["topic"],
            "background": cfg["background"],
            "attachments": cfg["attachments"],
            "config": cfg["config"],
            "rounds": [],
            "synthesis": None,
            "status": "running",
        }
        self._save(debate)
        run = Run(debate)
        with self._lock:
            self._runs[debate_id] = run
        run.thread = threading.Thread(target=self._run_debate, args=(run,), daemon=True, name=f"debate-{debate_id[:8]}")
        run.thread.start()
        return debate_id

    def get_run(self, debate_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(debate_id)

    def is_active(self, debate_id: str) -> bool:
        run = self.get_run(debate_id)
        return bool(run and run.active)

    def cancel(self, debate_id: str) -> bool:
        run = self.get_run(debate_id)
        if not run or not run.active:
            return False
        run.cancel_flag.set()
        return True

    def synthesize(self, debate_id: str) -> None:
        """对已结束/已取消的辩论（重新）生成综合报告，在后台运行，事件照常推送。"""
        if self.is_active(debate_id):
            raise ValidationError("辩论仍在进行中，请等待结束或先取消")
        debate = store.load_debate(debate_id)
        if not debate:
            raise KeyError(debate_id)
        if not any(m.get("status") == "ok" for r in debate.get("rounds", []) for m in r.get("messages", [])):
            raise ValidationError("还没有任何成功的发言，无法综合")
        syn = debate["config"]["synthesis"]
        if not providers.key_ok(syn["provider"]):
            raise ValidationError("综合者的 key 尚未配置，请先到设置页配置")
        run = Run(debate)
        with self._lock:
            self._runs[debate_id] = run
        run.thread = threading.Thread(target=self._run_synthesis_only, args=(run,), daemon=True)
        run.thread.start()

    def resume(self, debate_id: str, body: dict) -> None:
        """从历史里重新开启一场辩论（总轮数不设上限）。

        body: retry_failed（重试失败的发言）、extra_rounds（再追加几轮交叉辩论）、
              user_input（提问者的新补充，放进下一轮每个人的提示里）、
              participants [{id, model, role}]（换模型/角色，对之后的发言生效）、synthesis {provider, model}
        完成后重新综合。
        """
        if self.is_active(debate_id):
            raise ValidationError("辩论仍在进行中，请等待结束或先取消")
        debate = store.load_debate(debate_id)
        if not debate:
            raise KeyError(debate_id)
        body = body or {}
        retry = bool(body.get("retry_failed"))
        extra = _num(body.get("extra_rounds"), int, "追加轮数", 0, MAX_EXTRA_ROUNDS, 0)
        user_input = str(body.get("user_input") or "").strip()
        atts = attachments_of(body.get("attachments"))
        if (user_input or atts) and not extra:
            raise ValidationError("补充了新内容时，至少要再辩论 1 轮")
        cfg = debate["config"]

        by_id = {p["id"]: p for p in cfg["participants"]}
        for ch in body.get("participants") or []:
            p = by_id.get(str(ch.get("id") or ""))
            if not p:
                raise ValidationError(f"找不到参会者：{ch.get('id')}")
            if ch.get("model") is not None:
                p["model"] = str(ch["model"]).strip() or providers.default_model(p["provider"])
            if ch.get("role") is not None:
                p["role"] = str(ch["role"]).strip()
        if body.get("max_retries") is not None:
            cfg["max_retries"] = _num(body.get("max_retries"), int, "重试次数", 0, MAX_RETRIES, providers.DEFAULT_RETRIES)
        syn = body.get("synthesis")
        if isinstance(syn, dict) and syn.get("provider"):
            try:
                providers.get_provider(str(syn["provider"]))
            except ProviderError:
                raise ValidationError(f"未知的综合者 provider：{syn['provider']}") from None
            cfg["synthesis"] = {"provider": str(syn["provider"]),
                                "model": str(syn.get("model") or "").strip() or providers.default_model(str(syn["provider"]))}

        missing = sorted({providers.get_provider(p["provider"])["name"] for p in cfg["participants"]
                          if not providers.key_ok(p["provider"])})
        if missing:
            raise ValidationError(f"以下 provider 的 key 尚未配置：{'、'.join(missing)}。请先到设置页配置")
        if user_input or atts:
            item = {"before_round": len(debate["rounds"]), "text": user_input, "at": store.now_iso()}
            if atts:
                item["attachments"] = atts
            debate.setdefault("inputs", []).append(item)
        cfg["debate_rounds"] = max(cfg["debate_rounds"], len(debate["rounds"]) - 1) + extra
        debate["status"] = "running"
        debate.pop("error", None)
        self._save(debate)
        run = Run(debate)
        with self._lock:
            self._runs[debate_id] = run
        run.thread = threading.Thread(target=self._run_debate, args=(run, retry), daemon=True,
                                      name=f"debate-{debate_id[:8]}")
        run.thread.start()

    def delete(self, debate_id: str) -> bool:
        if self.is_active(debate_id):
            raise ValidationError("辩论仍在进行中，请先取消")
        with self._lock:
            self._runs.pop(debate_id, None)
        return store.delete_debate(debate_id)

    # -------- 内部

    def _save(self, debate: dict) -> None:
        debate["usage"] = store.usage_total(debate)
        store.save_debate(debate)

    def _call(self, run: Run, rnd_idx: int, msg: dict, system: str, user: str) -> None:
        """调用一位参会者，结果写回 msg（原地修改），并推送事件。"""
        cfg = run.debate["config"]
        pid = msg["participant"]
        msg["status"] = "running"
        run.emit("participant_started", {"round": rnd_idx, "participant": pid})

        def on_token(delta):
            run.emit("token", {"round": rnd_idx, "participant": pid, "delta": delta})

        def on_retry(note, wait=0):
            run.emit("participant_retry", {"round": rnd_idx, "participant": pid, "note": note,
                                           "retry_at": _retry_at(wait)})

        try:
            res = providers.chat(msg["provider"], msg["model"], system, user,
                                 cfg["max_tokens"], cfg["temperature"], True, on_token, on_retry,
                                 max_retries=cfg.get("max_retries"))
            text = (res.get("text") or "").strip()
            if not text:
                raise ProviderError("模型返回了空内容")
            msg.update(status="ok", text=text, usage=res.get("usage"))
            msg.pop("error", None)
            run.emit("message_done", {"round": rnd_idx, "participant": pid, "text": text, "usage": res.get("usage")})
        except ProviderError as e:
            msg.update(status="failed", text="", usage=None, error=e.msg)
            run.emit("message_failed", {"round": rnd_idx, "participant": pid, "error": e.msg})
        except Exception as e:  # noqa: BLE001 — 单个参会者的意外异常不能拖垮整场辩论
            log.exception("参会者 %s 调用异常", pid)
            msg.update(status="failed", text="", usage=None, error=f"内部错误：{type(e).__name__}: {e}")
            run.emit("message_failed", {"round": rnd_idx, "participant": pid, "error": msg["error"]})

    def _new_round(self, run: Run, idx: int) -> dict:
        rnd = {"index": idx, "messages": [
            {"participant": p["id"], "provider": p["provider"], "model": p["model"], "role": p["role"],
             "text": "", "status": "pending", "usage": None}
            for p in run.debate["config"]["participants"]
        ]}
        run.debate["rounds"].append(rnd)
        run.emit("round_started", {"round": idx, "title": round_title(idx, run.debate["config"]["blind_round"])})
        return rnd

    def _prompt(self, d: dict, m: dict, idx: int, prev: dict | None) -> tuple:
        system = BLIND_SYSTEM.format(role=m["role"] or DEFAULT_ROLE)
        if idx == 0:
            return system, blind_user(d["topic"], full_background(d))
        # 上下文裁剪：只带“其他人上一轮”的发言
        others = [x for x in prev["messages"] if x["status"] == "ok" and x["participant"] != m["participant"]]
        extra = input_before(d, idx)
        last_report = ""
        if extra:
            reps = [r for r in d.get("syntheses") or [] if r.get("after_round", -1) < idx]
            last_report = reps[-1]["text"] if reps else ""
        return system, cross_user(d["topic"], others, extra, last_report)

    def _run_round(self, run: Run, rnd: dict, prev: dict | None, only: set | None = None) -> None:
        """跑一轮；only 给定时只（重新）调用这些参会者。"""
        d = run.debate
        cfg = d["config"]
        idx = rnd["index"]
        msgs = [m for m in rnd["messages"] if only is None or m["participant"] in only]

        if idx == 0 and not cfg["blind_round"]:
            # 公开首轮：依次发言，后者看得到前者（重试时也能看到排在前面的成功发言）
            for m in msgs:
                pos = rnd["messages"].index(m)
                earlier = [x for x in rnd["messages"][:pos] if x["status"] == "ok"]
                system = OPEN_SYSTEM.format(role=m["role"] or DEFAULT_ROLE)
                self._call(run, idx, m, system, open_user(d["topic"], full_background(d), earlier))
                self._save(d)
            return

        # 同一个 provider 的参会者共用一份免费额度（按分钟算请求数/token），并发只会一起撞限流：
        # 不同 provider 之间并行，同一 provider 内依次调用
        groups: dict = {}
        for m in msgs:
            groups.setdefault(m["provider"], []).append(m)

        def run_group(group):
            for m in group:
                system, user = self._prompt(d, m, idx, prev)
                self._call(run, idx, m, system, user)
                self._save(d)

        if not groups:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(groups)), thread_name_prefix=f"r{idx}") as pool:
            for f in [pool.submit(run_group, g) for g in groups.values()]:
                f.result()

    def _synth_candidates(self, d: dict) -> list:
        """综合者候选：先用指定的综合者，失败再依次换参会者里其他（能用的）模型。"""
        syn = d["config"]["synthesis"]
        out = [(syn["provider"], syn["model"])]
        for p in d["config"]["participants"]:
            c = (p["provider"], p["model"])
            if c not in out and providers.key_ok(p["provider"]):
                out.append(c)
        return out

    def _synthesize(self, run: Run) -> bool:
        d = run.debate
        cands = self._synth_candidates(d)
        by = f"{cands[0][0]}/{cands[0][1]}"
        d["synthesis"] = {"text": "", "by": by, "status": "running"}
        run.emit("synthesis_started", {"by": by})

        def on_token(delta):
            run.emit("synthesis_token", {"delta": delta})

        def on_retry(note, wait=0):
            run.emit("synthesis_retry", {"note": note, "retry_at": _retry_at(wait)})

        # TODO(规格待确认)：综合报告较长，max_tokens 至少给 2000，避免四个章节被截断；温度不高于 0.7 以求忠实
        max_tokens = max(d["config"]["max_tokens"], 2000)
        temperature = min(d["config"]["temperature"], 0.7)
        user = synth_user(build_transcript(d))
        errors = []
        for i, (pid, model) in enumerate(cands):
            by = f"{pid}/{model}"
            if i:
                # TODO(规格待确认)：规格只说用指定综合者；免费档常 503/429，这里自动换参会者里的其他模型
                run.emit("synthesis_retry", {"note": f"{errors[-1]}；改由 {by} 综合", "by": by})
            try:
                res = providers.chat(pid, model, SYNTH_SYSTEM, user, max_tokens, temperature, True, on_token, on_retry,
                                     max_retries=d["config"].get("max_retries"))
                text = (res.get("text") or "").strip()
                if not text:
                    raise ProviderError("综合者返回了空内容")
                d["synthesis"] = {"text": text, "by": by, "status": "ok", "usage": res.get("usage"),
                                  "after_round": len(d["rounds"]) - 1, "at": store.now_iso()}
                if errors:
                    d["synthesis"]["fallback_from"] = errors
                # 保留每次综合的历史；同一进度上重新综合则替换最后一份
                hist = d.setdefault("syntheses", [])
                if hist and hist[-1].get("after_round") == d["synthesis"]["after_round"]:
                    hist[-1] = d["synthesis"]
                else:
                    hist.append(d["synthesis"])
                run.emit("synthesis_done", {"text": text, "by": by, "usage": res.get("usage")})
                return True
            except Exception as e:  # noqa: BLE001
                err = e.msg if isinstance(e, ProviderError) else f"内部错误：{type(e).__name__}: {e}"
                if not isinstance(e, ProviderError):
                    log.exception("综合失败")
                errors.append(f"{by} 失败：{err}")
        err = "；".join(errors)
        d["synthesis"] = {"text": "", "by": f"{cands[0][0]}/{cands[0][1]}", "status": "failed", "error": err}
        run.emit("synthesis_failed", {"error": err, "by": d["synthesis"]["by"]})
        return False

    def _run_debate(self, run: Run, retry_failed: bool = False) -> None:
        """跑完（或接着跑完）一场辩论：可选先重试失败的发言，再补齐计划中的轮次，最后综合。"""
        d = run.debate
        cfg = d["config"]
        run.emit("debate_started", {"debate_id": d["id"]})
        status = "done"
        try:
            if retry_failed:
                for i, rnd in enumerate(d["rounds"]):
                    if run.cancel_flag.is_set():
                        break
                    failed = {m["participant"] for m in rnd["messages"] if m["status"] != "ok"}
                    if not failed:
                        continue
                    cur = {p["id"]: p for p in cfg["participants"]}
                    for m in rnd["messages"]:
                        if m["participant"] in failed and m["participant"] in cur:
                            m["model"], m["role"] = cur[m["participant"]]["model"], cur[m["participant"]]["role"]
                    run.emit("round_started", {"round": rnd["index"], "title": round_title(rnd["index"], cfg["blind_round"]),
                                               "retry": True})
                    self._run_round(run, rnd, d["rounds"][i - 1] if i else None, only=failed)
                    self._save(d)
                    run.emit("round_done", {"round": rnd["index"]})

            planned = 1 + cfg["debate_rounds"]
            while len(d["rounds"]) < planned and not run.cancel_flag.is_set():
                prev = d["rounds"][-1] if d["rounds"] else None
                if prev is not None and not any(m["status"] == "ok" for m in prev["messages"]):
                    # 上一轮全部失败 → partial，中断后续轮次
                    status = "partial"
                    break
                rnd = self._new_round(run, len(d["rounds"]))
                self._run_round(run, rnd, prev)
                self._save(d)
                run.emit("round_done", {"round": rnd["index"]})
                if not any(m["status"] == "ok" for m in rnd["messages"]):
                    status = "partial"
                    break

            if run.cancel_flag.is_set():
                d["status"] = "cancelled"
                self._save(d)
                run.emit("debate_cancelled", {})
                return

            has_ok = any(m["status"] == "ok" for r in d["rounds"] for m in r["messages"])
            if has_ok:
                # TODO(规格待确认)：某轮全部失败时规格写“中断”，这里仍基于已成功的发言做综合，状态记为 partial
                if not self._synthesize(run):
                    status = "partial"
            else:
                status = "partial"
            d["status"] = status
            self._save(d)
            run.emit("debate_done", {"status": status})
        except Exception as e:  # noqa: BLE001
            log.exception("辩论 %s 异常终止", d["id"])
            d["status"] = "partial"
            d["error"] = f"{type(e).__name__}: {e}"
            self._save(d)
            run.emit("debate_done", {"status": "partial", "error": d["error"]})
        finally:
            run.finish()

    def _run_synthesis_only(self, run: Run) -> None:
        d = run.debate
        prev_status = d.get("status")
        try:
            ok = self._synthesize(run)
            planned_rounds = 1 + d["config"]["debate_rounds"]
            complete = len(d["rounds"]) >= planned_rounds and prev_status in ("done", "partial")
            if ok and complete:
                d["status"] = "done"
            elif prev_status == "running":
                d["status"] = "partial"
            self._save(d)
            run.emit("debate_done", {"status": d["status"]})
        except Exception as e:  # noqa: BLE001
            log.exception("综合 %s 异常", d["id"])
            run.emit("debate_done", {"status": d.get("status"), "error": str(e)})
        finally:
            run.finish()
