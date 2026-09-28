"""把导出的 Markdown 转成 Word（.docx）和可打印的 HTML（浏览器“另存为 PDF”），只用标准库。

只支持辩论记录里常见的 Markdown：标题、段落、列表、引用、代码块、表格、**粗体**、`代码`。
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from xml.sax.saxutils import escape

# ---------------------------------------------------------------- Markdown -> 块


def parse_blocks(md: str) -> list:
    """返回 [(类型, 内容)]：h1~h4 / p / li / ol / quote / code / table / hr。"""
    blocks, para, lines, i = [], [], md.replace("\r\n", "\n").split("\n"), 0

    def flush():
        if para:
            blocks.append(("p", " ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(code)))
        elif not s:
            flush()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", s):
            flush()
            blocks.append((f"h{min(len(m.group(1)), 4)}", m.group(2).strip()))
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush()
            blocks.append(("hr", ""))
        elif s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            rows = [_cells(s)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            blocks.append(("table", rows))
            continue
        elif m := re.match(r"^[-*+]\s+(.*)$", s):
            flush()
            blocks.append(("li", m.group(1)))
        elif m := re.match(r"^(\d+)[.)、]\s*(.*)$", s):
            flush()
            blocks.append(("ol", f"{m.group(1)}. {m.group(2)}"))
        elif s.startswith(">"):
            flush()
            blocks.append(("quote", s.lstrip(">").strip()))
        else:
            para.append(s)
        i += 1
    flush()
    return blocks


def _cells(row: str) -> list:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def inline_runs(text: str) -> list:
    """[(文字, 粗体, 代码)]"""
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append((part[1:-1], False, True))
        else:
            out.append((part, False, False))
    return out


# ---------------------------------------------------------------- HTML（打印成 PDF）

def _inline_html(text: str) -> str:
    out = []
    for t, bold, code in inline_runs(text):
        t = html.escape(t)
        out.append(f"<strong>{t}</strong>" if bold else f"<code>{t}</code>" if code else t)
    return "".join(out)


PRINT_CSS = """
body { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif; color: #222;
  max-width: 820px; margin: 24px auto; padding: 0 20px; line-height: 1.65; font-size: 15px; }
h1 { font-size: 24px; border-bottom: 2px solid #c2410c; padding-bottom: 6px; }
h2 { font-size: 20px; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
h3 { font-size: 17px; margin-top: 22px; } h4 { font-size: 15px; margin-top: 16px; color: #444; }
blockquote { margin: 8px 0; padding: 4px 12px; border-left: 3px solid #c2410c; color: #555; background: #faf6f2; }
code { background: #f3f1ee; padding: 1px 4px; border-radius: 3px; font-size: 90%; }
pre { background: #f3f1ee; padding: 10px; border-radius: 6px; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 8px 0; } td, th { border: 1px solid #ccc; padding: 4px 8px; }
ul, ol { padding-left: 22px; margin: 6px 0; }
.bar { position: sticky; top: 0; background: #fff8f1; border: 1px solid #f0d9c4; padding: 8px 12px; border-radius: 8px;
  display: flex; gap: 10px; align-items: center; font-size: 13px; }
.bar button { padding: 5px 12px; border-radius: 6px; border: 1px solid #c2410c; background: #c2410c; color: #fff; cursor: pointer; }
@media print { .bar { display: none; } body { margin: 0 auto; } h2, h3, h4 { break-after: avoid; } }
"""


def to_html(md: str, title: str) -> str:
    body, lst = [], None
    for kind, val in parse_blocks(md):
        want = "ul" if kind == "li" else "ol" if kind == "ol" else None
        if lst and want != lst:
            body.append(f"</{lst}>")
            lst = None
        if want and not lst:
            body.append(f"<{want}>")
            lst = want
        if kind in ("h1", "h2", "h3", "h4"):
            body.append(f"<{kind}>{_inline_html(val)}</{kind}>")
        elif kind == "p":
            body.append(f"<p>{_inline_html(val)}</p>")
        elif kind == "li":
            body.append(f"<li>{_inline_html(val)}</li>")
        elif kind == "ol":
            body.append(f"<li>{_inline_html(val.split('. ', 1)[1])}</li>")
        elif kind == "quote":
            body.append(f"<blockquote>{_inline_html(val)}</blockquote>")
        elif kind == "code":
            body.append(f"<pre>{html.escape(val)}</pre>")
        elif kind == "hr":
            body.append("<hr>")
        elif kind == "table":
            rows = "".join("<tr>" + "".join(f"<{'th' if r == 0 else 'td'}>{_inline_html(c)}</{'th' if r == 0 else 'td'}>"
                                            for c in row) + "</tr>" for r, row in enumerate(val))
            body.append(f"<table>{rows}</table>")
    if lst:
        body.append(f"</{lst}>")
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{PRINT_CSS}</style></head><body>"
        "<div class='bar'><button onclick='print()'>打印 / 另存为 PDF</button>"
        "<span>在打印窗口的“目标打印机”里选择“另存为 PDF”。</span></div>"
        + "\n".join(body)
        + "<script>addEventListener('load', () => setTimeout(() => print(), 300));</script></body></html>"
    )


# ---------------------------------------------------------------- Word（.docx）

_FONT = '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei" w:cs="Calibri"/>'


def _runs(text: str, bold=False) -> str:
    out = []
    for t, b, code in inline_runs(text):
        props = ""
        if b or bold:
            props += "<w:b/>"
        if code:
            props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:shd w:val="clear" w:fill="F3F1EE"/>'
        out.append(f"<w:r>{'<w:rPr>' + props + '</w:rPr>' if props else ''}"
                   f'<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')
    return "".join(out)


def _p(inner: str, style: str | None = None, ppr: str = "") -> str:
    st = f'<w:pStyle w:val="{style}"/>' if style else ""
    return f"<w:p><w:pPr>{st}{ppr}</w:pPr>{inner}</w:p>"


def _docx_body(md: str) -> str:
    out = []
    for kind, val in parse_blocks(md):
        if kind == "h1":
            out.append(_p(_runs(val), "Title"))
        elif kind in ("h2", "h3", "h4"):
            out.append(_p(_runs(val), f"Heading{int(kind[1]) - 1}"))
        elif kind == "p":
            out.append(_p(_runs(val)))
        elif kind == "li":
            out.append(_p('<w:r><w:t xml:space="preserve">•  </w:t></w:r>' + _runs(val), None,
                          '<w:ind w:left="420" w:hanging="260"/>'))
        elif kind == "ol":
            n, rest = val.split(". ", 1)
            out.append(_p(f'<w:r><w:t xml:space="preserve">{escape(n)}.  </w:t></w:r>' + _runs(rest), None,
                          '<w:ind w:left="420" w:hanging="300"/>'))
        elif kind == "quote":
            out.append(_p(_runs(val), "Quote"))
        elif kind == "code":
            for ln in val.split("\n") or [""]:
                out.append(_p(f'<w:r><w:t xml:space="preserve">{escape(ln)}</w:t></w:r>', "Code"))
        elif kind == "hr":
            out.append(_p("", None, '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="CCCCCC"/></w:pBdr>'))
        elif kind == "table":
            cols = max(len(r) for r in val)
            rows = []
            for r, row in enumerate(val):
                cells = "".join(
                    f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>{_p(_runs(c, bold=(r == 0)))}</w:tc>'
                    for c in row + [""] * (cols - len(row)))
                rows.append(f"<w:tr>{cells}</w:tr>")
            border = "".join(f'<w:{s} w:val="single" w:sz="4" w:color="BBBBBB"/>'
                             for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
            out.append(f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>'
                       f'<w:tblGrid>{"<w:gridCol/>" * cols}</w:tblGrid>{"".join(rows)}</w:tbl>')
            out.append(_p(""))
    return "".join(out)


# 注意：Word 对 pPr/rPr 子元素顺序很严格（pStyle、keepNext、pBdr、shd、spacing、ind、outlineLvl），改样式时保持这个顺序
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {_W_NS}>
<w:docDefaults><w:rPrDefault><w:rPr>{_FONT}<w:sz w:val="22"/><w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="12" w:space="4" w:color="C2410C"/></w:pBdr>
<w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="C2410C"/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:color w:val="444444"/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
<w:pPr><w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="C2410C"/></w:pBdr><w:ind w:left="360"/></w:pPr><w:rPr><w:color w:val="555555"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:fill="F3F1EE"/>
<w:spacing w:after="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="20"/></w:rPr></w:style>
</w:styles>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def to_docx(md: str, title: str) -> bytes:
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {_W_NS}><w:body>'
           f'{_docx_body(md)}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1440" w:right="1260" w:bottom="1440" w:left="1260" w:header="720" w:footer="720" w:gutter="0"/>'
           "</w:sectPr></w:body></w:document>")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>AI 议事厅</dc:creator></cp:coreProperties>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()
