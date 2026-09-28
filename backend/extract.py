"""从上传的文档里提取纯文本：txt / md / docx / pdf，只用标准库。

PDF 只做尽力而为的文字提取（支持 Flate 压缩、对象流、ToUnicode 映射的中文字体）；
扫描件（图片）、加密 PDF 无法提取，会给出中文提示。
"""
from __future__ import annotations

import io
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 10 * 1024 * 1024
TEXT_LIMIT = 20000   # 每个附件最多发给模型的字数（免费档按 token 限流，太长会直接 429）
MAX_FILES = 5
TEXT_EXTS = (".txt", ".md", ".markdown", ".csv", ".json", ".log")
SUPPORTED = "txt、md、pdf、docx"


class ExtractError(Exception):
    pass


def extract(name: str, data: bytes) -> str:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if len(data) > MAX_FILE_BYTES:
        raise ExtractError(f"文件太大（上限 {MAX_FILE_BYTES // 1024 // 1024} MB）")
    if ext in TEXT_EXTS:
        text = decode_text(data)
    elif ext == ".docx":
        text = docx_text(data)
    elif ext == ".pdf":
        text = pdf_text(data)
    elif ext == ".doc":
        raise ExtractError("暂不支持旧版 .doc，请在 Word 里另存为 .docx 再上传")
    else:
        raise ExtractError(f"暂不支持 {ext or '这种'} 文件，目前支持 {SUPPORTED}")
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        if ext == ".pdf":
            raise ExtractError("没能从这个 PDF 里提取出文字，可能是扫描件（图片），暂不支持识别")
        raise ExtractError("文件里没有文字内容")
    return text


# ---------------------------------------------------------------- 纯文本

def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "gb18030"):  # Windows 中文记事本常见 GBK
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ExtractError("不是有效的 .docx 文件") from None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ExtractError("docx 内容无法解析") from None
    lines = []
    for p in root.iter(_W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == _W + "t":
                parts.append(el.text or "")
            elif el.tag == _W + "tab":
                parts.append("\t")
            elif el.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------- pdf

_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_DELIMS = b" \t\r\n\f\x00/<>[]()%{}"


def _inflate(raw: bytes):
    for wbits in (15, -15):
        try:
            return zlib.decompressobj(wbits).decompress(raw if wbits > 0 else raw[2:])
        except zlib.error:
            continue
    return None


def _ref(d: bytes, key: bytes):
    m = re.search(rb"/" + key + rb"\s*(\d+)\s+\d+\s+R", d)
    return int(m.group(1)) if m else None


def _int(d: bytes, key: bytes):
    m = re.search(rb"/" + key + rb"\s+(\d+)(?!\s+\d+\s+R)", d)
    return int(m.group(1)) if m else None


class _Pdf:
    def __init__(self, data: bytes):
        self.objs = {}   # 对象号 -> (字典部分, 原始流 或 None)
        for m in _OBJ.finditer(data):
            body = m.group(3)
            sm = re.search(rb"stream\r?\n", body)
            if sm:
                raw = body[sm.end():]
                end = raw.rfind(b"endstream")
                self.objs[int(m.group(1))] = (body[:sm.start()], raw[:end] if end >= 0 else raw)
            else:
                self.objs[int(m.group(1))] = (body, None)
        # PDF 1.5+ 常把字体、页面等对象压进对象流（ObjStm）
        for num, (d, raw) in list(self.objs.items()):
            if raw is None or not re.search(rb"/Type\s*/ObjStm", d):
                continue
            s = self.stream(num)
            n, first = _int(d, b"N"), _int(d, b"First")
            if not s or n is None or first is None:
                continue
            head = s[:first].split()
            pairs = [(int(head[i]), int(head[i + 1])) for i in range(0, min(len(head), 2 * n) - 1, 2)]
            for k, (onum, off) in enumerate(pairs):
                stop = pairs[k + 1][1] if k + 1 < len(pairs) else len(s) - first
                self.objs.setdefault(onum, (s[first + off:first + stop], None))
        self._fonts = {}

    def dict(self, num):
        o = self.objs.get(num)
        return o[0] if o else b""

    def stream(self, num):
        o = self.objs.get(num)
        if not o or o[1] is None:
            return None
        d, raw = o
        if b"/Filter" not in d:
            return raw
        if re.search(rb"/Filter\s*\[?\s*/FlateDecode\s*\]?", d):
            return _inflate(raw)
        return None  # 图片等其他编码，与文字无关

    def pages(self):
        out, seen = [], set()

        def walk(num):
            if num in seen or num not in self.objs:
                return
            seen.add(num)
            d = self.dict(num)
            if re.search(rb"/Type\s*/Pages\b", d):
                km = re.search(rb"/Kids\s*\[(.*?)\]", d, re.S)
                for k in re.findall(rb"(\d+)\s+\d+\s+R", km.group(1) if km else b""):
                    walk(int(k))
            elif re.search(rb"/Type\s*/Page\b", d):
                out.append(num)

        for num in sorted(self.objs):
            if re.search(rb"/Type\s*/Catalog\b", self.dict(num)):
                root = _ref(self.dict(num), b"Pages")
                if root is not None:
                    walk(root)
        if not out:
            out = [n for n in sorted(self.objs) if re.search(rb"/Type\s*/Page\b", self.dict(n))]
        return out

    def contents(self, page):
        d = self.dict(page)
        m = re.search(rb"/Contents\s*\[(.*?)\]", d, re.S)
        refs = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))] if m else []
        if not m:
            r = _ref(d, b"Contents")
            if r is not None:
                if self.objs.get(r, (b"", None))[1] is None:  # 间接引用的数组
                    refs = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", self.dict(r))]
                else:
                    refs = [r]
        return b"\n".join(s for s in (self.stream(r) for r in refs) if s)

    def font_map(self, page):
        """页面资源里的字体名 -> (映射表 或 None, 编码字节数)。资源可能继承自上级 Pages。"""
        num, seen = page, set()
        while num is not None and num not in seen:
            seen.add(num)
            d = self.dict(num)
            res = d
            rr = _ref(d, b"Resources")
            if rr is not None:
                res = self.dict(rr)
            fr = re.search(rb"/Font\s+(\d+)\s+\d+\s+R", res)
            fd = self.dict(int(fr.group(1))) if fr else None
            if fd is None:
                fm = re.search(rb"/Font\s*<<(.*?)>>", res, re.S)
                fd = fm.group(1) if fm else None
            if fd is not None:
                return {name.decode("latin-1"): self.font(int(ref))
                        for name, ref in re.findall(rb"/([^\s/<>\[\]()]+)\s*(\d+)\s+\d+\s+R", fd)}
            num = _ref(d, b"Parent")
        return {}

    def font(self, num):
        if num not in self._fonts:
            d = self.dict(num)
            tu = _ref(d, b"ToUnicode")
            cmap, width = (_parse_cmap(self.stream(tu) or b"") if tu is not None else (None, 1))
            if re.search(rb"/Subtype\s*/Type0", d):
                width = 2 if not cmap else width
                if not cmap:
                    cmap = {}  # 无映射的复合字体无法还原文字
            self._fonts[num] = (cmap, width)
        return self._fonts[num]


def _u16(h: bytes) -> str:
    try:
        return bytes.fromhex(h.decode("ascii")).decode("utf-16-be", errors="ignore")
    except ValueError:
        return ""


def _parse_cmap(data: bytes):
    m, width = {}, None
    for block in re.findall(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        hx = re.findall(rb"<([0-9A-Fa-f]+)>", block)
        if hx:
            width = max(1, len(hx[0]) // 2)
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            m[int(src, 16)] = _u16(dst)
            width = width or max(1, len(src) // 2)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            width = width or max(1, len(lo) // 2)
            lo, hi = int(lo, 16), int(hi, 16)
            if hi - lo > 65535:
                continue
            if dst.startswith(b"["):
                for i, x in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", dst)[:hi - lo + 1]):
                    m[lo + i] = _u16(x)
            else:
                h = dst[1:-1]
                if not h:
                    continue
                base, size = int(h, 16), len(h) // 2
                for i in range(hi - lo + 1):
                    try:
                        m[lo + i] = (base + i).to_bytes(size, "big").decode("utf-16-be", errors="ignore")
                    except OverflowError:
                        break
    return m, width or 1


def _literal(buf: bytes, i: int):
    """解析 (...) 字面量字符串，i 指向左括号；返回 (字节, 结束位置)。"""
    out, depth, i, n = bytearray(), 1, i + 1, len(buf)
    esc = {110: 10, 114: 13, 116: 9, 98: 8, 102: 12}
    while i < n:
        c = buf[i]
        if c == 92:  # 反斜杠
            i += 1
            if i >= n:
                break
            c = buf[i]
            if c in esc:
                out.append(esc[c])
            elif 48 <= c <= 55:
                j = i
                while j < n and j < i + 3 and 48 <= buf[j] <= 55:
                    j += 1
                out.append(int(buf[i:j], 8) & 0xFF)
                i = j - 1
            elif c == 13:
                if i + 1 < n and buf[i + 1] == 10:
                    i += 1
            elif c != 10:
                out.append(c)
        elif c == 40:
            depth += 1
            out.append(c)
        elif c == 41:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    return bytes(out), n


def _tokens(buf: bytes):
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c in b" \t\r\n\f\x00":
            i += 1
        elif c == 37:  # % 注释
            while i < n and buf[i] not in b"\r\n":
                i += 1
        elif c == 40:
            s, i = _literal(buf, i)
            yield ("s", s)
        elif c == 60:
            if buf[i + 1:i + 2] == b"<":
                i += 2
                yield ("<<", None)
            else:
                j = buf.find(b">", i)
                j = n if j < 0 else j
                hx = re.sub(rb"[^0-9A-Fa-f]", b"", buf[i + 1:j])
                if len(hx) % 2:
                    hx += b"0"
                yield ("s", bytes.fromhex(hx.decode("ascii")))
                i = j + 1
        elif c == 62:
            i += 2 if buf[i + 1:i + 2] == b">" else 1
            yield (">>", None)
        elif c in b"[]":
            i += 1
            yield (chr(c), None)
        elif c in b"{}":
            i += 1
        else:
            j = i + 1
            while j < n and buf[j] not in _DELIMS:
                j += 1
            tok = buf[i:j]
            i = j
            if c == 47:
                yield ("n", tok[1:].decode("latin-1"))
            elif re.fullmatch(rb"[+-]?(\d+\.?\d*|\.\d+)", tok):
                yield ("f", float(tok))
            else:
                op = tok.decode("latin-1")
                if op == "ID":  # 内嵌图片：跳过二进制数据
                    m = re.compile(rb"\sEI(?=[\s]|$)").search(buf, i)
                    i = m.end() if m else n
                    continue
                yield ("op", op)


def _decode(s: bytes, font) -> str:
    if font is None or font[0] is None:
        return "".join(chr(b) for b in s if b >= 32 or b in (9, 10))
    cmap, width = font
    out = []
    for k in range(0, len(s) - width + 1, width):
        code = int.from_bytes(s[k:k + width], "big")
        ch = cmap.get(code)
        if ch is None and width == 1 and code >= 32:
            ch = chr(code)
        out.append(ch or "")
    return "".join(out)


def _page_text(content: bytes, fonts: dict) -> str:
    out, stack, arrays = [], [], []
    font, last_y = None, None

    def put(t):
        if t:
            out.append(t)

    def newline():
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    for kind, val in _tokens(content):
        if kind == "[":
            arrays.append(stack)
            stack = []
            continue
        if kind == "]":
            arr = stack
            stack = arrays.pop() if arrays else []
            stack.append(("a", arr))
            continue
        if kind != "op":
            stack.append((kind, val))
            continue
        ops = stack
        stack = []
        if val == "Tf" and len(ops) >= 2 and ops[-2][0] == "n":
            font = fonts.get(ops[-2][1])
        elif val in ("Tj", "'", '"'):
            if val != "Tj":
                newline()
            if ops and ops[-1][0] == "s":
                put(_decode(ops[-1][1], font))
        elif val == "TJ" and ops and ops[-1][0] == "a":
            for k, v in ops[-1][1]:
                if k == "s":
                    put(_decode(v, font))
                elif k == "f" and v < -250 and out and out[-1][-1:].isascii() and out[-1][-1:].isalnum():
                    put(" ")
        elif val in ("Td", "TD") and len(ops) >= 2 and ops[-1][0] == "f":
            if abs(ops[-1][1]) > 0.5:
                newline()
        elif val == "T*":
            newline()
        elif val == "Tm" and len(ops) >= 6 and ops[-1][0] == "f":
            y = ops[-1][1]
            if last_y is not None and abs(y - last_y) > 0.5:
                newline()
            last_y = y
        elif val == "ET":
            pass
    return "".join(out)


def pdf_text(data: bytes) -> str:
    if not data.startswith(b"%PDF"):
        raise ExtractError("不是有效的 PDF 文件")
    if re.search(rb"/Encrypt\s*(\d+\s+\d+\s+R|<<)", data):
        raise ExtractError("PDF 已加密，请先去掉密码保护再上传")
    pdf = _Pdf(data)
    pages = []
    for p in pdf.pages():
        pages.append(_page_text(pdf.contents(p), pdf.font_map(p)).strip())
    return "\n\n".join(t for t in pages if t)
