#!/usr/bin/env python3
"""pdftool.py — PDF 工具箱：AI 友好化 / OCR / 正文与表格 / 拆分合并 / 表单 / 图片。

设计意图：把 PDF 处理里那些机械、易错、重复的活儿从模型的工作流里搬进脚本——
判断有没有文字层、抽正文、认表格、抠图片、加 OCR 文字层、按章拆分、填表单——
模型只负责决定"该做哪一步"，而不是每次现写一段 PyMuPDF 代码。

两条主线：
  * **AI 友好化**（to-ai）：把一本书变成 书.md + 页锚全文 + manifest.json，
    每条内容都带 [[p.N]] 页锚（N = PDF 物理页号），便于回查原书。
  * **OCR 文字层**（ocr）：给扫描版加上可搜索的隐藏文字层，
    让扫描书也能走文本路线、引文能被机器核对（见 ocrtool.py）。

用法速查
    python pdftool.py check
    python pdftool.py info     <pdf> [--json]
    python pdftool.py text     <pdf> [--pages 5-12] [--mode raw|blocks|markdown]
    python pdftool.py search   <pdf> --pattern 关键词 [--pages 100-200]
    python pdftool.py to-ai    <pdf> --out DIR [--images] [--tables] [--ocr]
    python pdftool.py ocr      <pdf> [--out out.pdf] [--pages R] [--sidecar f.txt]
    python pdftool.py tables   <pdf> [--pages R] --out DIR
    python pdftool.py images   <pdf> --out DIR [--render-pages]
    python pdftool.py render   <pdf> --pages R [--dpi 300]
    python pdftool.py split    <pdf> --by-bookmarks --out DIR
    python pdftool.py merge    --out all.pdf a.pdf b.pdf
    python pdftool.py pages    <pdf> --out new.pdf [--select R] [--delete R]
    python pdftool.py forms    <pdf> [--list|--template t.json|--fill d.json --out f.pdf]
    python pdftool.py outline  <pdf> [--json|--md]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdfcommon as C  # noqa: E402

# 扫描仪/OCR 流水线生成的占位书签名，如 cov001 / fow012 / !00001 / 000355
SCAN_ARTIFACT_RE = re.compile(r"^[!_]?[a-z]{0,4}\d{1,6}$", re.I)

# 纯页码/装饰性页码行（页眉页脚里最常见）
PAGE_NUM_RE = re.compile(r"^[\s\d\-—–·.、|IVXLCivxlc]{1,12}$")

# 章节标题的文字特征（字号不可靠时靠它兜底：OCR 出的页面字号都一样）
HEADING_PATTERNS = [
    (1, re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]{1,6}\s*[章篇部卷]")),
    (2, re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]{1,6}\s*[节講]")),
    (1, re.compile(r"^(序|序言|前言|绪论|导论|引言|结语|结论|后记|附录|参考文献|目录|索引|致谢)\s*$")),
    (1, re.compile(r"^(Chapter|Part|Appendix|Contents)\s+[\dIVXivx]+", re.I)),
    (2, re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]\s*\S")),
    (3, re.compile(r"^[（(][一二三四五六七八九十\d]{1,3}[)）]\s*\S")),
    (3, re.compile(r"^\d{1,2}(\.\d{1,2}){1,3}\s+\S")),
]

CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
SENTENCE_END = "。！？…；;!?”\"」）】》"
LIST_START = re.compile(r"^\s*([·•▪◆◇▲△■□○●\-–—*]|\d+[.)、]|[（(]\d+[)）])")


# ==========================================================================
# 页面内容分析：把一页拆成"有顺序的正文/标题/表格/图片"
# ==========================================================================
def _join_lines(lines: list[str]) -> str:
    """把段落内的多行拼成一个段落：中文之间不加空格，西文之间加空格。"""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        prev = out[-1]
        # 西文断词：行尾的连字符丢掉
        if prev == "-" and line[:1].islower():
            out = out[:-1] + line
            continue
        if CJK_RE.match(prev) or CJK_RE.match(line[0]):
            out += line
        else:
            out += " " + line
    return out


def _block_text(block: dict) -> tuple[str, float, bool]:
    """文本块 → (文字, 最大字号, 是否含粗体)。"""
    lines, sizes, bold = [], [], False
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            if not text.strip():
                parts.append(text)
                continue
            parts.append(text)
            sizes.append((round(float(span.get("size", 0)), 1), len(text)))
            if int(span.get("flags", 0)) & 16:
                bold = True
        lines.append("".join(parts))
    max_size = max((s for s, _ in sizes), default=0.0)
    return _join_lines(lines), max_size, bold


def body_font_size(blocks: list[dict]) -> float:
    """正文基准字号：按字符数加权的众数（标题字数少，压不过正文）。"""
    counter: Counter = Counter()
    for b in blocks:
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text.strip():
                    counter[round(float(span.get("size", 0)), 1)] += len(text)
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


def classify_heading(text: str, size: float, bold: bool, body: float,
                     allow_size: bool = True) -> tuple[int, str] | None:
    """判断一行是不是标题，返回 (Markdown 级别 1-4, 判定依据) 或 None。

    allow_size=False 用于 OCR 出来的文字：那些"字号"是我们自己按 OCR 框高度
    估的，表格里字大一点就会被误判成标题，反而把表内容当成章节名。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return None
    if stripped[-1] in SENTENCE_END and len(stripped) > 12:
        return None
    if not allow_size:
        # OCR 文字：只认"第X章/第X节/Chapter N"这类文字特征，且要求短
        if len(stripped) > 40 or stripped[-1:] in "，、：;,":
            return None
    elif body > 0:
        ratio = size / body if body else 1.0
        if ratio >= 1.6:
            return 1, "size"
        if ratio >= 1.3:
            return 2, "size"
        if ratio >= 1.12:
            return 3, "size"
        if bold and len(stripped) <= 30 and ratio >= 1.0:
            return 4, "size"
    for level, pattern in HEADING_PATTERNS:
        if pattern.match(stripped):
            return level, "pattern"
    return None


def page_has_ocr_layer(page) -> bool:
    """这一页的文字层是不是本工具 OCR 写上去的。

    我们自己写的隐藏文字用的是 refname "ocr-cjk" 的字体，查字体表就能认出来。
    为什么需要这个判断：OCR 出来的文字没有真实字号，靠"字号比正文大"来判标题
    会把表格里的内容当成章节名，所以这类页面要换成只认文字特征。
    """
    try:
        for font in page.get_fonts(full=True):
            if len(font) > 4 and "ocr-cjk" in str(font[4]):
                return True
    except Exception:
        pass
    return False


def _quiet(fn, *a, **kw):
    """压掉第三方库的 stdout/stderr 闲聊。

    PyMuPDF 的 find_tables 会在每次进程里打一句"考虑用 pymupdf_layout"的提示，
    这会混进脚本给模型看的输出里，让人分不清哪句是结果、哪句是库在唠叨。
    """
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return fn(*a, **kw)
    finally:
        buf.close()


def find_tables_silent(page, kwargs: dict):
    try:
        return page.find_tables(**kwargs)
    except TypeError:
        return page.find_tables()


def _in_rect(inner: tuple, outer: tuple, ratio: float = 0.6) -> bool:
    """内框有 ratio 以上面积落在外框里就算"在内"。"""
    x0 = max(inner[0], outer[0])
    y0 = max(inner[1], outer[1])
    x1 = min(inner[2], outer[2])
    y1 = min(inner[3], outer[3])
    if x1 <= x0 or y1 <= y0:
        return False
    inter = (x1 - x0) * (y1 - y0)
    area = max(1e-6, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / area >= ratio


def find_page_tables(page, min_rows: int = 2, min_cols: int = 2,
                     strategy: str | None = None) -> list[dict]:
    """抽这一页的表格（find_tables 基于线条/文本对齐推断）。"""
    kwargs = {}
    if strategy:
        kwargs["strategy"] = strategy
    try:
        found = _quiet(find_tables_silent, page, kwargs)
    except Exception as exc:
        C.warn(f"第 {page.number + 1} 页表格识别失败：{exc}")
        return []
    tables = []
    for idx, table in enumerate(getattr(found, "tables", []) or [], start=1):
        try:
            rows = table.extract()
        except Exception:
            continue
        rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in row]
                for row in (rows or [])]
        rows = [r for r in rows if any(c for c in r)]
        if len(rows) < min_rows or (rows and len(rows[0]) < min_cols):
            continue
        header = None
        try:
            head = table.header
            names = getattr(head, "names", None)
            if names:
                header = [str(n) for n in names]
        except Exception:
            header = None
        if header and rows and [c.strip() for c in header] == [c.strip() for c in rows[0]]:
            body = rows[1:]
        else:
            body = rows
        tables.append({"idx": idx, "bbox": tuple(table.bbox), "header": header,
                       "rows": body, "row_count": len(body),
                       "col_count": max((len(r) for r in rows), default=0)})
    return tables


def page_image_placements(page, min_px: int = 40, skip_page_scans: bool = True):
    """页面上的位图摆放信息（用于把插图写进 Markdown）。

    必须用 xrefs=True：不带它时返回的字典里没有 xref，后面就没法把原图抠出来。
    """
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return []
    area = abs(page.rect.width * page.rect.height) or 1.0
    out = []
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        w, h = info.get("width", 0), info.get("height", 0)
        if w < min_px or h < min_px:
            continue
        box_area = max(1e-6, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        if skip_page_scans and box_area / area >= 0.85:
            continue  # 整页扫描底图不是插图
        out.append({"bbox": tuple(bbox), "width": w, "height": h,
                    "size": info.get("size", 0), "cs": info.get("cs-name", ""),
                    "xref": info.get("xref") or info.get("number")})
    return out


def analyze_page(page, *, tables: bool = False, min_rows: int = 2, min_cols: int = 2,
                 table_strategy: str | None = None, drop_headers: bool = True,
                 drop_footers: bool = True, min_image_px: int = 40,
                 skip_page_scans: bool = True, figures: bool = True,
                 allow_size_headings: bool = True, synthetic: bool = False) -> dict:
    """分析一页：返回 {"items": [...有序内容...], "tables": [...], "images": [...]}。

    items 里每项形如 {"kind": "heading|para|table|figure", "y0": float, ...}，
    按纵坐标排序，尽量还原"从上到下"的阅读顺序。
    """
    pymupdf = C.import_pymupdf()
    height = abs(page.rect.height) or 1.0
    raw = page.get_text("dict")
    blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
    body = body_font_size(blocks)

    table_list = find_page_tables(page, min_rows, min_cols, table_strategy) if tables else []
    image_list = page_image_placements(page, min_image_px, skip_page_scans) if figures else []

    items: list[dict] = []
    for b in blocks:
        bbox = tuple(b.get("bbox", (0, 0, 0, 0)))
        text, size, bold = _block_text(b)
        if not text.strip():
            continue
        n_lines = len(b.get("lines", []))
        # 页眉页脚：贴边 + 短文本（书名/章节名/页码）
        if (drop_headers and bbox[3] <= height * 0.055 and len(text) <= 60) or \
           (drop_footers and bbox[1] >= height * 0.945 and len(text) <= 60):
            continue
        if PAGE_NUM_RE.match(text.strip()) and len(text.strip()) <= 12:
            continue
        if any(_in_rect(bbox, t["bbox"], 0.6) for t in table_list):
            continue  # 表格里的文字已由 find_tables 抽出，避免重复
        verdict = classify_heading(text, size, bold, body,
                                   allow_size=allow_size_headings) if n_lines <= 2 else None
        level, why = (verdict if verdict else (None, ""))
        items.append({"kind": "heading" if verdict else "para", "level": level or 0,
                      "strict": why == "pattern", "synthetic": synthetic,
                      "y0": bbox[1], "y1": bbox[3], "x0": bbox[0], "x1": bbox[2],
                      "text": text, "lines": n_lines, "size": size})
    for t in table_list:
        items.append({"kind": "table", "y0": t["bbox"][1], "x0": t["bbox"][0],
                      "x1": t["bbox"][2], "table": t, "synthetic": synthetic})
    if figures:
        for img in image_list:
            items.append({"kind": "figure", "y0": img["bbox"][1], "x0": img["bbox"][0],
                          "x1": img["bbox"][2], "image": img, "synthetic": synthetic})

    items.sort(key=lambda it: (round(it["y0"], 1), it["x0"]))
    items = _attach_captions(items, page)
    items = reflow_paragraphs(items, page)
    return {"items": items, "tables": table_list, "images": image_list,
            "body_size": body}


CAPTION_RE = re.compile(r"^\s*(图|表|插图|Figure|Fig\.?|Table|Tab\.?)\s*[\d一二三四五六七八九十]")


def _attach_captions(items: list[dict], page) -> list[dict]:
    """把紧跟插图下方的"图 1-2 ……"文字挂到对应的图上，作为图注。"""
    figures = [it for it in items if it["kind"] == "figure"]
    if not figures:
        return items
    used = set()
    for fig in figures:
        bottom = max(fig["image"]["bbox"][3], fig["y0"])
        best = None
        for it in items:
            if it["kind"] != "para" or id(it) in used:
                continue
            if not CAPTION_RE.match(it["text"]):
                continue
            gap = it["y0"] - bottom
            if -6 <= gap <= 60 and (best is None or gap < best[0]):
                best = (gap, it)
        if best:
            fig["caption"] = best[1]["text"].strip()
            used.add(id(best[1]))
    return [it for it in items if id(it) not in used]


def _pct(values_sorted: list[float], ratio: float) -> float:
    """取已排序列表的分位值（空列表返回 0）。"""
    if not values_sorted:
        return 0.0
    idx = max(0, min(len(values_sorted) - 1, int(round(ratio * (len(values_sorted) - 1)))))
    return values_sorted[idx]


def reflow_paragraphs(items: list[dict], page) -> list[dict]:
    """把被切断的段落拼回去。

    这里要处理两种"碎"法，两种都不能靠"这一行排满了没有"来判断：

    1. OCR 出来的页：每行一个文本对象；
    2. 有些 PDF（含本工具自己生成的演示件）PyMuPDF 会把一个自然段切成**多个多行块**
       （例如第 1-2 行一块、第 3-4 行一块），块内 lines>1，但块与块之间仍是同一段。

    原先用"整页最大行宽"判断行有没有排满；但两端对齐（justified）的正文里每一行
    都顶到右边距，最大值与常见值没有区分度，判据失效、长段落被切碎。
    真正可靠的信号是**标点**：段落最后一行以句末标点收尾，被切断的行不会；
    行距与左边界只是辅助。所以合并条件改成"上一块结尾没有句末标点 + 左边界对齐 +
    行距正常 + 下一块不是列表/标题"。
    """
    paras = [it for it in items if it["kind"] == "para"]
    if not paras:
        return items
    page_width = abs(page.rect.width) or 1.0
    # 行高用"块高 / 块内行数"估计，块可能是多行的
    heights = sorted((it.get("y1", it["y0"]) - it["y0"]) / max(1, it.get("lines", 1))
                     for it in paras if it.get("y1", it["y0"]) > it["y0"])
    line_h = _pct(heights, 0.5) or 12.0

    merged: list[dict] = []
    for it in items:
        if it["kind"] != "para" or not merged:
            merged.append(it)
            continue
        prev = merged[-1]
        if prev["kind"] != "para":
            merged.append(it)
            continue
        # y1 是块底边；旧版本只存了 y0，这里做兼容（缺 y1 时按行高估算）
        prev_bottom = prev.get("y1", prev["y0"] + line_h * max(1, prev.get("lines", 1)))
        gap = it["y0"] - prev_bottom            # 上一块底 → 本块顶
        same_column = abs(it["x0"] - prev["x0"]) <= page_width * 0.02
        tight = gap <= line_h * 0.9            # 正常行距；明显加大的间距=新段
        ends_soft = prev["text"][-1:] not in SENTENCE_END
        starts_plain = not LIST_START.match(it["text"]) and \
            classify_heading(it["text"], it.get("size", 0), False, 0,
                             allow_size=False) is None
        if same_column and tight and ends_soft and starts_plain:
            prev["text"] = _join_lines([prev["text"], it["text"]])
            prev["lines"] = prev.get("lines", 1) + it.get("lines", 1)
            prev["x1"] = max(prev["x1"], it["x1"])
            prev["y1"] = it["y1"]
            continue
        merged.append(it)
    return merged


# ==========================================================================
# Markdown / 纯文本渲染
# ==========================================================================
def render_items_markdown(items: list[dict], page_no: int, opts: dict,
                          image_map: dict | None = None) -> str:
    out: list[str] = []
    for it in items:
        if it["kind"] == "heading":
            level = min(4, max(1, it["level"] or 2))
            out.append(f"{'#' * level} {it['text'].strip()}")
            out.append("")
        elif it["kind"] == "para":
            text = it["text"].strip()
            if not text:
                continue
            out.append(text)
            out.append("")
        elif it["kind"] == "table":
            table = it["table"]
            if opts.get("tables_md", True):
                out.append(C.md_table(table["rows"], header=table.get("header")))
                out.append("")
        elif it["kind"] == "figure":
            img = it["image"]
            caption = it.get("caption", "")
            rel = ""
            if image_map:
                rel = image_map.get(id(img)) or image_map.get(
                    (page_no, round(img["bbox"][1], 1)), "")
            label = caption or f"PDF p.{page_no} 插图"
            if rel:
                out.append(f"![{label}]({rel})")
            else:
                out.append(f"![{label}]（图片未导出：加 --images 可一并提取）")
            if caption:
                out.append(f"*{caption}*")
            out.append("")
    return "\n".join(out).strip()


def page_plain_text(page, mode: str = "raw") -> str:
    if mode == "blocks":
        try:
            blocks = page.get_text("blocks")
        except Exception:
            return page.get_text()
        blocks = sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))
        return "\n".join(str(b[4]).strip() for b in blocks if str(b[4]).strip())
    return page.get_text()


def outline_is_degenerate(toc: list) -> tuple[bool, float]:
    """判断大纲是不是"有书签但没用"。

    扫描件常带一份由扫描流水线生成的逐页书签，名字形如 fow001 / 000123，
    页数对得上但完全不是章节名。把它当目录用会白白浪费一整轮阅读。
    """
    if not toc:
        return True, 0.0
    junk = 0
    for entry in toc:
        title = str(entry[1]).strip()
        if SCAN_ARTIFACT_RE.match(title) and not re.search(r"[\u4e00-\u9fff]", title):
            junk += 1
    ratio = junk / len(toc)
    return ratio >= 0.6, ratio


def chapter_map(toc: list, page_count: int, max_level: int = 2) -> list[dict]:
    """书签 → 章节页范围列表。"""
    entries = [(int(lvl), str(title).strip(), int(pg))
               for lvl, title, pg in (toc or [])
               if int(lvl) <= max_level and 1 <= int(pg) <= page_count]
    tops = [e for e in entries if e[0] == 1] or entries
    out = []
    for i, (lvl, title, start) in enumerate(tops):
        end = tops[i + 1][2] - 1 if i + 1 < len(tops) else page_count
        if end < start:
            end = start
        out.append({"level": lvl, "title": title, "start": start, "end": end,
                    "pages": end - start + 1})
    return out


def heading_map_from_pages(pages_items: list[tuple[int, dict]],
                           max_level: int = 2) -> list[dict]:
    """没有书签时，用检测到的"第X章"标题拼一份结构地图。

    OCR 页面上的标题只认文字特征匹配的（strict）；靠字号判出来的多半是
    表格内容或图注，放进结构地图只会让人以为书里有这些章。
    """
    hits: list[tuple[int, str, int]] = []
    for page_no, info in pages_items:
        for it in info.get("items", []):
            if it["kind"] != "heading" or (it.get("level") or 9) > max_level:
                continue
            if it.get("synthetic") and not it.get("strict"):
                continue
            text = it["text"].strip()
            if len(text) > 50 or not text:
                continue
            if hits and hits[-1][1] == text:
                continue
            hits.append((it.get("level") or 1, text, page_no))
    if not hits:
        return []
    out = []
    for i, (lvl, title, start) in enumerate(hits):
        end = hits[i + 1][2] - 1 if i + 1 < len(hits) else hits[-1][2]
        out.append({"level": lvl, "title": title, "start": start,
                    "end": max(start, end), "pages": max(1, end - start + 1)})
    return out


# ==========================================================================
# 子命令：check / info
# ==========================================================================
def cmd_check(args):
    pymupdf = None
    try:
        pymupdf = C.import_pymupdf()
    except SystemExit:
        pass
    print("pdf-toolkit 环境自检")
    print(f"  python      : {sys.version.split()[0]}（{sys.executable}）")
    print(f"  pymupdf     : {getattr(pymupdf, 'version', None)}")
    print("  OCR 引擎    :")
    for eng in __import__("ocrtool").detect_engines():
        mark = "可用" if eng["available"] else "不可用"
        print(f"    - {eng['name']:<10} {mark:<6} {eng['detail']}")
    print(f"  pdftotext   : {C.find_pdftotext() or '未找到（可选，用于对比提取质量）'}")
    cpus = os.cpu_count() or 4
    print(f"  CPU         : {cpus} 核（OCR 建议 --jobs {max(1, min(6, cpus // 3))}）")
    print("  说明        : OCR 用 pip 装的 RapidOCR（离线、自带中文模型）；"
          "装了 tesseract/ocrmypdf 会自动优先使用")
    return 0


def cmd_info(args):
    doc = C.open_doc(args.pdf, password=args.password)
    path = C.norm_path(args.pdf)
    texts, method = C.extract_page_texts(doc, args.method, pdf_path=path)
    assess = C.assess_text(texts)
    toc = doc.get_toc(simple=True)
    degenerate, junk_ratio = outline_is_degenerate(toc)
    page_count = doc.page_count

    pages = [doc[i] for i in range(page_count)] if page_count <= 800 else \
        [doc[i] for i in range(0, page_count, max(1, page_count // 200))]
    sizes = Counter(f"{round(p.rect.width)}x{round(p.rect.height)}" for p in pages)
    rotations = Counter(p.rotation for p in pages)
    n_images = sum(len(p.get_images(full=True)) for p in pages)
    if len(pages) != page_count:
        n_images = int(n_images / len(pages) * page_count)
    n_annots = 0
    for p in pages:
        try:
            n_annots += len(list(p.annots() or []))
        except Exception:
            pass
    n_forms = 0
    if getattr(doc, "is_form_pdf", 0):
        seen = set()
        for p in pages:
            for w in (p.widgets() or []):
                key = (w.field_name, round(w.rect.x0), round(w.rect.y0))
                if key not in seen:
                    seen.add(key)
                    n_forms += 1

    info = {
        "file": path,
        "name": Path(path).name,
        "size_mb": round(os.path.getsize(path) / 1048576, 2),
        "pages": page_count,
        "encrypted": bool(getattr(doc, "needs_pass", False)),
        "metadata": {k: v for k, v in (doc.metadata or {}).items()
                     if v and k in ("title", "author", "subject", "keywords",
                                    "creationDate", "producer")},
        "page_sizes": dict(sizes),
        "rotations": {str(k): v for k, v in rotations.items()},
        "text_layer": {**assess, "extractor": method},
        "outline": {"entries": len(toc), "degenerate": degenerate,
                    "junk_ratio": round(junk_ratio, 3),
                    "top_level": [{"title": str(t).strip(), "page": p}
                                  for lvl, t, p in toc if lvl == 1][:30]},
        "features": {"form_fields": n_forms, "images": n_images,
                     "annotations": n_annots,
                     "is_form_pdf": int(getattr(doc, "is_form_pdf", 0) or 0),
                     "xfa": bool(doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0]
                                 not in ("null", "none", None))},
    }
    advice = []
    verdict = assess["verdict"]
    if verdict == "text-rich":
        advice.append("文字层可用 → 直接抽正文：pdftool.py text / to-ai。")
    elif verdict == "text-partial":
        advice.append("文字层部分可用 → 正文走文本路线，图版页用 render 出图再视觉判读。")
    else:
        advice.append(f"文字层不可用（{verdict}）→ 先加 OCR 文字层："
                      f"pdftool.py ocr \"{Path(path).name}\" --out <输出.pdf> "
                      f"--sidecar <全书.pages.txt>"
                      "（扫描书加完 OCR 就能走文本路线，引文也能被机器核对）")
    if info["outline"]["degenerate"] and info["outline"]["entries"]:
        advice.append("PDF 的书签是扫描流水线生成的占位名，不能当目录用 → "
                      "结构要么 OCR 后从正文识别，要么视觉阅读取印刷目录。")
    elif not info["outline"]["entries"]:
        advice.append("PDF 没有书签大纲 → 结构地图需要另外确定（render 目录页视觉判读，"
                      "或用 to-ai 从正文标题推断）。")
    else:
        advice.append(f"书签大纲可用（{info['outline']['entries']} 条）→ "
                      "outline --chapters 可直接拿到章节页范围。")
    if n_forms:
        advice.append(f"检测到 {n_forms} 个表单字段 → pdftool.py forms --list 查看，"
                      "--template 生成填写模板。")
    if info["features"]["xfa"]:
        advice.append("这是 XFA 表单：PyMuPDF 只能列出字段，填不了内容，"
                      "请用 Adobe Acrobat 之类的工具填写。")
    if n_images:
        advice.append(f"共约 {n_images} 个内嵌位图 → pdftool.py images --out DIR 提取。")
    if recommend_tables(doc, verdict):
        advice.append("这本书像是有排版的表格 → pdftool.py tables 可抽成 CSV/Markdown。")
    info["advice"] = advice

    if args.out:
        out = Path(C.norm_path(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        doc.close()
        return 0

    print(f"文件      : {info['name']}（{info['size_mb']} MB）")
    print(f"页数      : {info['pages']}")
    if info["metadata"]:
        print(f"元数据    : {json.dumps(info['metadata'], ensure_ascii=False)}")
    print(f"页面尺寸  : {json.dumps(info['page_sizes'], ensure_ascii=False)}")
    print(f"文字层    : {verdict}（提取器 {method}）")
    print(f"            每页均 {assess['per_page_chars']} 字｜有文字 "
          f"{assess['pages_with_text']}/{assess['pages']} 页｜中文占比 {assess['cjk_ratio']}")
    outline_note = ""
    if info["outline"]["entries"]:
        outline_note = ("（⚠ 多为扫描占位名，不可当目录）"
                        if info["outline"]["degenerate"] else "")
    print(f"书签大纲  : {info['outline']['entries']} 条{outline_note}")
    print(f"表单/图片 : 表单字段 {n_forms} 个；内嵌图片约 {n_images} 个；"
          f"注释 {n_annots} 个")
    print("建议      :")
    for a in advice:
        print(f"  - {a}")
    doc.close()
    return 0


def recommend_tables(doc, verdict: str) -> bool:
    """粗判这本书是不是有"线框表格"（有矢量线条的页面才可能命中）。"""
    if verdict == "scanned":
        return False
    hits = 0
    for i in range(min(doc.page_count, 25)):
        try:
            if doc[i].get_drawings():
                hits += 1
        except Exception:
            continue
    return hits >= 3


# ==========================================================================
# 子命令：text / to-ai
# ==========================================================================
def cmd_text(args):
    doc = C.open_doc(args.pdf, password=args.password)
    page_count = doc.page_count
    pages = C.parse_pages(args.pages, page_count, offset=args.offset,
                          default_n=args.default_pages)
    if not pages:
        C.die("页范围为空（扫描版页码换算用 --offset）")
    if args.mode == "markdown":
        chunks = []
        for n in pages:
            info = analyze_page(doc[n - 1], tables=args.tables)
            body = render_items_markdown(info["items"], n, {})
            chunks.append(f"{C.PAGE_MARK.format(n)}\n{body}\n")
        text = "\n".join(chunks)
    else:
        parts = []
        for n in pages:
            parts.append(f"{C.PAGE_MARK.format(n)}\n"
                         f"{page_plain_text(doc[n - 1], args.mode).rstrip()}\n")
        text = "\n".join(parts)
    if args.out:
        out = Path(C.norm_path(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"已写出 {len(pages)} 页 → {out}（{len(text)} 字符）")
    else:
        print(text)
    doc.close()
    return 0


def build_manifest_common(doc, path: str) -> dict:
    return {
        "tool": "pdf-toolkit/pdftool.py",
        "generated": C.now_stamp(),
        "source": {
            "file": path,
            "name": Path(path).name,
            "pages": doc.page_count,
            "size_mb": round(os.path.getsize(path) / 1048576, 2),
            "metadata": {k: v for k, v in (doc.metadata or {}).items()
                         if v and k in ("title", "author", "subject", "creationDate")},
        },
    }


def cmd_to_ai(args):
    src = C.ensure_pdf(args.pdf)
    doc = C.open_doc(src, password=args.password)
    page_count = doc.page_count
    slug = C.slugify(args.title or Path(src).stem)
    out_dir = Path(C.norm_path(args.out)) if args.out else \
        Path(src).parent / f"{slug}_ai"
    out_dir.mkdir(parents=True, exist_ok=True)

    texts, method = C.extract_page_texts(doc, args.method, pdf_path=src)
    assess = C.assess_text(texts)
    mode = assess["verdict"]
    ocr_used = None
    ocr_page_source: dict[int, str] = {}   # 页号 → "ocr" | "text" | "none"

    # 扫描版：可选先做 OCR，然后基于 OCR 后的文件继续
    if mode in ("scanned", "text-garbled") and args.ocr:
        import ocrtool
        ocr_pdf = out_dir / f"{slug}_ocr.pdf"
        est_min = page_count * 4.5 / max(1, args.jobs) / 60
        est = "（不到 1 分钟）" if est_min < 1 else f"（约 {round(est_min)} 分钟）"
        print(f"先给这本书加 OCR 文字层{est}：{ocr_pdf}")
        pages_scope = None
        if args.pages:
            pages_scope = C.parse_pages(args.pages, page_count, offset=args.offset)
        st = ocrtool.ocr_document(src, out_pdf=str(ocr_pdf), pages=pages_scope,
                                  dpi=args.dpi, engine=args.engine, jobs=args.jobs,
                                  min_conf=args.min_conf, password=args.password,
                                  quiet=True)
        ocrtool.print_report(st)
        ocr_used = str(ocr_pdf)
        ocr_page_source = {p: v["source"] for p, v in st["results"].items()}
        doc.close()
        doc = C.open_doc(str(ocr_pdf), password=args.password)
        texts, method = C.extract_page_texts(doc, "pymupdf", pdf_path=str(ocr_pdf))
        assess = C.assess_text(texts)
    elif mode in ("scanned", "text-garbled") and not args.ocr:
        print(f"⚠ 这本书文字层判定为 {mode}：正文暂时读不到文字。"
              "加 --ocr 可以让本工具先做 OCR 再打包（扫描版必须这样），"
              "或单独跑 pdftool.py ocr 生成可搜索 PDF。")

    pages = C.parse_pages(args.pages, page_count, offset=args.offset,
                          default_n=page_count)
    images_dir = out_dir / "images"
    tables_dir = out_dir / "tables"
    image_map: dict = {}
    table_files: list[dict] = []
    page_reports: list[dict] = []

    # 第一趟：逐页分析（顺便把图片/表格落盘）
    # OCR 出来的文字层没有真实字号信息，靠"字号比正文大"判定标题会误判，
    # 所以这类页面只认"第X章/第X节"这类文字特征。
    synthetic_book = mode in ("scanned", "text-garbled")
    analyzed: list[tuple[int, dict]] = []
    # 输入本身可能已经带了我们写进去的 OCR 隐藏文字层（例如上一轮 ocr 产出的 _ocr.pdf）。
    # 这种页的文字不是原书的可信文字层，manifest 的 source 必须标成 ocr，
    # 否则下游会把 OCR 文本当成可逐字引用的正文——见 page_has_ocr_layer 的说明。
    existing_ocr_layer: dict[int, bool] = {}
    for n in pages:
        page = doc[n - 1]
        has_ocr_layer = page_has_ocr_layer(page)
        existing_ocr_layer[n] = has_ocr_layer
        synthetic = (synthetic_book or ocr_page_source.get(n) == "ocr"
                     or has_ocr_layer)
        info = analyze_page(page, tables=args.tables, min_rows=args.min_rows,
                            min_cols=args.min_cols, table_strategy=args.table_strategy,
                            drop_headers=not args.keep_headers,
                            drop_footers=not args.keep_headers,
                            min_image_px=args.min_image_px,
                            skip_page_scans=not args.page_scans_are_figures,
                            allow_size_headings=not synthetic, synthetic=synthetic)
        analyzed.append((n, info))

    if args.images:
        images_dir.mkdir(parents=True, exist_ok=True)
        for n, info in analyzed:
            for k, img in enumerate(info["images"], start=1):
                rel = extract_one_image(doc, n, img, images_dir, k, args)
                if rel:
                    image_map[id(img)] = rel

    if args.tables:
        tables_dir.mkdir(parents=True, exist_ok=True)
        for n, info in analyzed:
            for t in info["tables"]:
                rel = write_table_file(t, tables_dir, n, args.table_format)
                t["file"] = rel
                t["page"] = n
                table_files.append({"page": n, "idx": t["idx"], "file": rel,
                                    "rows": t["row_count"], "cols": t["col_count"],
                                    "header": t.get("header")})

    # 第二趟：按页写 Markdown + 记录 manifest
    md_parts: list[str] = []
    for n, info in analyzed:
        body = render_items_markdown(info["items"], n, {}, image_map)
        if not body.strip():
            body = ("（本页没有可提取的文字：扫描图版页，需视觉阅读——"
                    "用 pdftool.py render 出图后判读，或用 ocr 加文字层）")
            head_source = "none"
        elif ocr_page_source.get(n) == "ocr":
            # 本次内联 OCR 新加的文字层
            head_source = "ocr"
        elif existing_ocr_layer.get(n):
            # 输入文件里本来就有的 OCR 文字层（同一次 ocr 的产物，或用户给的 _ocr.pdf）
            head_source = "ocr"
        else:
            head_source = "text" if (n < len(texts) and texts[n].strip()) else "none"
        md_parts.append(f"{C.PAGE_MARK.format(n)}\n\n{body}\n")
        page_reports.append({
            "page": n,
            "chars": len(texts[n]) if n < len(texts) else 0,
            "source": head_source,
            "headings": [it["text"].strip() for it in info["items"]
                         if it["kind"] == "heading"],
            "tables": [{"idx": t["idx"], "file": t.get("file"),
                        "rows": t["row_count"], "cols": t["col_count"]}
                       for t in info["tables"]],
            "images": [{"file": image_map.get(id(img), ""), "w": img["width"],
                        "h": img["height"], "caption": cap}
                       for img, cap in ((i, i.get("caption", ""))
                                        for i in info["images"])],
        })

    toc = doc.get_toc(simple=True)
    degenerate, junk_ratio = outline_is_degenerate(toc)
    if toc and not degenerate:
        cmap = chapter_map(toc, page_count, max_level=2)
        cmap_source = "PDF 书签"
    else:
        cmap = heading_map_from_pages(analyzed, max_level=2)
        cmap_source = "正文标题推断"

    header = [f"# {args.title or Path(src).stem}", ""]
    header.append(f"> 来源：{Path(src).name}｜{page_count} 页｜"
                  f"本次打包 {len(pages)} 页｜文字层 {mode}"
                  + ("（已用 OCR 补文字层）" if ocr_used else ""))
    header.append(f"> 页锚：`[[p.N]]` 中的 N 是 **PDF 物理页号**（从 1 开始），"
                  "回查时直接翻到第 N 页")
    header.append(f"> 生成：pdf-toolkit to-ai（{C.now_stamp()}）")
    if any(r["source"] == "none" for r in page_reports):
        header.append("> 注意：标注「没有可提取的文字」的页面需要视觉阅读，"
                      "本文件里没有它们的正文")
    header.append("")
    if cmap:
        header.append(f"## 结构地图（来源：{cmap_source}）")
        header.append("")
        for c in cmap:
            indent = "  " * max(0, c["level"] - 1)
            header.append(f"{indent}- {c['title']}（PDF p.{c['start']}–p.{c['end']}，"
                          f"{c['pages']} 页）")
        header.append("")
    md_text = "\n".join(header) + "\n---\n\n" + "\n".join(md_parts)

    md_path = out_dir / f"{slug}.md"
    md_path.write_text(md_text, encoding="utf-8", newline="\n")

    pages_txt = None
    if not args.no_pages_txt:
        pairs = [(n, texts[n] if n < len(texts) else "") for n in pages]
        pages_txt = C.write_pages_txt(out_dir / f"{slug}.pages.txt", pairs)

    manifest = build_manifest_common(doc, src)
    manifest.update({
        "text_layer": {**assess, "extractor": method},
        "ocr_pdf": ocr_used,
        "outline": {"entries": len(toc), "degenerate": degenerate,
                    "junk_ratio": round(junk_ratio, 3)},
        "structure_map": {"source": cmap_source, "chapters": cmap},
        "options": {"images": bool(args.images), "tables": bool(args.tables),
                    "dpi": args.dpi},
        "outputs": {
            "markdown": str(md_path),
            "pages_txt": str(pages_txt) if pages_txt else None,
            "tables_dir": str(tables_dir) if args.tables else None,
            "images_dir": str(images_dir) if args.images else None,
        },
        "counts": {"tables": len(table_files),
                   "images": len(image_map),
                   "pages_without_text": sum(1 for r in page_reports
                                             if r["source"] == "none")},
        "tables": table_files,
        "pages": page_reports,
    })
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"AI 友好包已生成 → {out_dir}")
    print(f"  {md_path.name}            Markdown 正文（{len(md_text)} 字符）"
          f"{'，含结构地图' if cmap else ''}")
    if pages_txt:
        print(f"  {pages_txt.name}        带 [[p.N]] 页锚的全文"
              "（可直接当 book-kb-builder 的 --source）")
    print("  manifest.json        机器可读索引（每页的标题/表格/图片/文字来源）")
    if args.tables:
        print(f"  tables/              {len(table_files)} 张表 → CSV/Markdown")
    if args.images:
        print(f"  images/              {len(image_map)} 张插图")
    if manifest["counts"]["pages_without_text"]:
        print(f"  ⚠ 其中 {manifest['counts']['pages_without_text']} 页没有文字层"
              "（扫描图版页，需视觉阅读或 OCR）")
    if not cmap:
        print("  ⚠ 没能确定章节结构：PDF 无书签、正文也没识别出章节标题；"
              "可先 ocr 再加文字层试试")
    print()
    print("下一步：")
    if pages_txt:
        print(f"  - 把这本书读进知识库：交给 book-kb-builder skill，"
              f"--source 用 {pages_txt}"
              + (f"（书用 OCR 后的 {Path(ocr_used).name}）" if ocr_used else ""))
    if not pages_txt:
        print(f"  - 需要页锚全文当校对源时，去掉 --no-pages-txt 重跑，"
              f"或用 pdftool.py ocr --sidecar 生成")
    if manifest["counts"]["pages_without_text"]:
        print("  - 图版/无文字页：pdftool.py render \"<pdf>\" --pages <页> --dpi 300 出图后视觉判读")
    if not ocr_used and mode in ("scanned", "text-garbled"):
        print("  - 正文读不到字：pdftool.py ocr \"<pdf>\" --out <输出.pdf> "
              "--sidecar <pages.txt>（扫描版必做），或本命令加 --ocr")
    doc.close()
    return 0


def extract_one_image(doc, page_no: int, img: dict, out_dir: Path, seq: int,
                      args) -> str:
    """导出页面上的一张图，返回相对路径（失败返回空串）。"""
    pymupdf = C.import_pymupdf()
    page = doc[page_no - 1]
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    xref = None
    for info in infos:
        if tuple(info.get("bbox", ())) == tuple(img["bbox"]):
            xref = info.get("xref")
            break
    try:
        if xref:
            raw = doc.extract_image(xref)
        else:
            raise ValueError("没找到 xref")
        data = raw.get("image", b"")
        ext = raw.get("ext", "png")
    except Exception:
        # 退路：直接把这块区域渲染出来（矢量图/遮罩图都适用）
        try:
            clip = pymupdf.Rect(*img["bbox"])
            pix = page.get_pixmap(clip=clip, dpi=args.dpi)
            data, ext = pix.tobytes("png"), "png"
        except Exception as exc:
            C.warn(f"第 {page_no} 页的图片导出失败：{exc}")
            return ""
    if len(data) < args.min_image_kb * 1024:
        return ""
    name = f"p{page_no:04d}_{seq}.{ext}"
    target = out_dir / name
    target.write_bytes(data)
    return f"images/{name}"


def write_table_file(table: dict, out_dir: Path, page_no: int,
                     fmt: str = "csv") -> str:
    """把一张表写成 CSV（utf-8-sig，Excel 打开中文不乱码）。"""
    base = f"p{page_no:04d}_t{table['idx']}"
    rows = ([table["header"]] if table.get("header") else []) + table["rows"]
    if fmt in ("csv", "all"):
        path = out_dir / f"{base}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh).writerows(rows)
    if fmt == "md":
        path = out_dir / f"{base}.md"
        path.write_text(C.md_table(table["rows"], header=table.get("header")),
                        encoding="utf-8", newline="\n")
    return f"tables/{path.name}"


# ==========================================================================
# 子命令：ocr / tables / images / render
# ==========================================================================
def cmd_ocr(args):
    import ocrtool
    src = C.ensure_pdf(args.pdf)
    out = None
    if not args.no_pdf:
        if args.out:
            out_path = Path(C.norm_path(args.out))
            if out_path.is_dir():
                out_path = out_path / f"{Path(src).stem}_ocr.pdf"
        else:
            out_path = Path(src).parent / f"{Path(src).stem}_ocr.pdf"
        out = C.unique_path(out_path) if not args.force else out_path
    doc = C.open_doc(src, password=args.password)
    pages = C.parse_pages(args.pages, doc.page_count, offset=args.offset,
                          default_n=doc.page_count) if args.pages else None
    doc.close()
    seco = Path(out).suffix.lower() if out else ""
    if out and seco != ".pdf":
        C.die(f"OCR 产物必须是 PDF（当前 {out.name}）。只想导出文本请加 --no-pdf "
              "并配合 --sidecar。")
    stats = ocrtool.ocr_document(
        src, out_pdf=str(out) if out else None, pages=pages, dpi=args.dpi,
        engine=args.engine, jobs=args.jobs, min_conf=args.min_conf,
        existing=args.existing, dry_run=args.dry_run, password=args.password,
        sidecar=args.sidecar, quiet=False)
    ocrtool.print_report(stats)
    if not args.dry_run:
        print()
        print("提示：")
        if out:
            print(f"  - 产物可直接交给 book-kb-builder（probe 会判成 text-rich/partial），"
                  f"文件名保持 _ocr 好辨认")
        if args.sidecar:
            print(f"  - {args.sidecar} 是整册 [[p.N]] 全文，"
                  "可当 verify.py 的 --source（注意 OCR 会有识别误差，"
                  "引文对不上时先回原页面图核对）")
    if args.json:
        keep = {k: v for k, v in stats.items() if k != "results"}
        print(json.dumps(keep, ensure_ascii=False, indent=2))
    return 0


def cmd_tables(args):
    doc = C.open_doc(args.pdf, password=args.password)
    pages = C.parse_pages(args.pages, doc.page_count, offset=args.offset,
                          default_n=doc.page_count)
    out_dir = Path(C.norm_path(args.out)) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    for n in pages:
        for t in find_page_tables(doc[n - 1], args.min_rows, args.min_cols,
                                  args.table_strategy):
            entry = {"page": n, "idx": t["idx"], "rows": t["row_count"],
                     "cols": t["col_count"], "header": t.get("header"),
                     "bbox": [round(v, 1) for v in t["bbox"]],
                     "markdown": C.md_table(t["rows"], header=t.get("header"))}
            if out_dir:
                entry["file"] = str(out_dir / Path(
                    write_table_file(t, out_dir, n, args.format)).name)
            found.append(entry)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
    else:
        if not found:
            print(f"在 {len(pages)} 页里没找到表格。")
            print("  可能原因：这是扫描版（没有矢量线条，表格要视觉判读或 OCR 后人工整理）；"
                  "或表格没有边框（试试 --strategy text）。")
        for e in found:
            print(f"  p.{e['page']} 第{e['idx']}张：{e['rows']} 行 × {e['cols']} 列"
                  f"｜表头 {e['header']}｜{e.get('file', '')}")
        if args.print and found:
            print()
            for e in found:
                print(entry_md(e))
                print()
    doc.close()
    return 0 if found else 1


def entry_md(entry: dict) -> str:
    return f"<!-- p.{entry['page']} 表{entry['idx']} -->\n{entry['markdown']}"


def cmd_images(args):
    doc = C.open_doc(args.pdf, password=args.password)
    pages = C.parse_pages(args.pages, doc.page_count, offset=args.offset,
                          default_n=doc.page_count)
    # 没给 --out 时落到与原件同目录的 <文件名>_images/，和 to-ai 的 <文件名>_ai/ 一致
    out_target = args.out or (Path(C.norm_path(args.pdf)).parent /
                              f"{C.slugify(Path(C.norm_path(args.pdf)).stem, 40)}_images")
    out_dir = Path(C.norm_path(str(out_target)))
    if not args.list:
        out_dir.mkdir(parents=True, exist_ok=True)
    pymupdf = C.import_pymupdf()
    seen_digest: dict[str, str] = {}
    rows, written = [], 0
    skipped = Counter()
    dry = args.list  # --list 只列清单，不落盘

    if args.render_pages:
        for n in pages:
            pix = doc[n - 1].get_pixmap(dpi=args.dpi, colorspace=pymupdf.csRGB,
                                        alpha=False)
            path = out_dir / f"p{n:04d}.png"
            if not dry:
                pix.save(str(path))
            rows.append({"page": n, "file": path.name, "w": pix.width,
                         "h": pix.height, "kb": 0 if dry else round(path.stat().st_size / 1024)})
            written += 1
    else:
        for n in pages:
            page = doc[n - 1]
            page_area = abs(page.rect.width * page.rect.height) or 1.0
            for idx, item in enumerate(page.get_image_info(xrefs=True), start=1):
                bbox = item.get("bbox") or (0, 0, 0, 0)
                box_area = max(1e-6, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                if args.skip_page_scans and box_area / page_area >= 0.85:
                    skipped["整页扫描底图"] += 1
                    continue
                if item.get("width", 0) < args.min_px or item.get("height", 0) < args.min_px:
                    skipped[f"尺寸小于 {args.min_px}px"] += 1
                    continue
                xref = item.get("xref") or 0
                if not xref:
                    skipped["取不到对象编号"] += 1
                    continue
                try:
                    raw = doc.extract_image(xref)
                except Exception:
                    skipped["对象不是可直接导出的位图"] += 1
                    continue
                data, ext = raw.get("image", b""), raw.get("ext", "png")
                if len(data) < args.min_kb * 1024:
                    skipped[f"小于 {args.min_kb} KB"] += 1
                    continue
                digest = hashlib.sha1(data).hexdigest()
                if args.dedupe and digest in seen_digest:
                    rows.append({"page": n, "file": seen_digest[digest],
                                 "w": raw.get("width"), "h": raw.get("height"),
                                 "kb": round(len(data) / 1024), "dup": True})
                    continue
                name = f"p{n:04d}_{idx:02d}.{ext}"
                if not dry:
                    (out_dir / name).write_bytes(data)
                if args.dedupe:
                    seen_digest[digest] = name
                rows.append({"page": n, "file": name, "w": raw.get("width"),
                             "h": raw.get("height"), "kb": round(len(data) / 1024)})
                written += 1
    if args.json:
        payload = {"file": C.norm_path(args.pdf), "out": str(out_dir),
                   "written": written, "images": rows}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for r in rows[:args.show]:
            print(f"  p.{r['page']:<5} {r['file']:<18} {r.get('w')}x{r.get('h')}  "
                  f"{r['kb']} KB{'（重复，已跳过）' if r.get('dup') else ''}")
        if len(rows) > args.show:
            print(f"  …（共 {len(rows)} 条，只显示前 {args.show} 条）")
        if args.list:
            print(f"共 {len(rows)} 张图（--list 只列清单，没有写文件）")
        else:
            print(f"已写出 {written} 个文件 → {out_dir}")
            if skipped:
                detail = "、".join(f"{k} {v} 张" for k, v in skipped.most_common())
                print(f"  跳过的图：{detail}")
                print("  （觉得漏了想要的图，可调小 --min-kb / --min-px，"
                      "或用 render 出整页图看）")
            if written and not args.render_pages:
                print("  说明：这些是 PDF 里的**原始位图**（可能含扫描底图、图标）；"
                      "矢量插图抓不到，用 render 出整页图看。")
    doc.close()
    return 0


def cmd_search(args):
    """在文字层里找关键词，返回页号 + 上下文——先定位、再精读，别通读全书。"""
    doc = C.open_doc(args.pdf, password=args.password)
    texts, method = C.extract_page_texts(doc, args.method, pdf_path=C.norm_path(args.pdf))
    assess = C.assess_text(texts)
    if assess["verdict"] in ("scanned", "text-garbled") and not args.force:
        doc.close()
        print(f"⚠ 文字层判定为 {assess['verdict']}，无法全文检索。")
        print("  先加文字层：pdftool.py ocr \"<书>\" --sidecar <pages.txt>；"
              "或改走视觉路线（render 出页面图来读）。")
        return 1
    try:
        pattern = re.compile(args.pattern, re.I if args.ignore_case else 0)
    except re.error as exc:
        C.die(f"正则不合法: {exc}")
        return 2
    needle = C.compact(args.pattern) if not args.regex else None
    hits = 0
    printed_pages = set()
    for n, text in enumerate(texts[1:], start=1):
        if not text:
            continue
        if args.pages and n not in args.pages:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            matched = bool(pattern.search(line))
            if not matched and needle and needle in C.compact(line):
                matched = True  # 提取时被插入空白的情况
            if not matched:
                continue
            hits += 1
            printed_pages.add(n)
            lo = max(0, i - args.context)
            hi = min(len(lines), i + args.context + 1)
            print(C.PAGE_MARK.format(n))
            for j in range(lo, hi):
                print(f"{'>' if j == i else ' '} {lines[j]}")
            print()
            if hits >= args.max_hits:
                print(f"（已达 --max-hits {args.max_hits}，要更多请调高上限）")
                doc.close()
                return 0
            break  # 每页只报第一处，避免同页刷屏
    doc.close()
    if not hits:
        print(f"未命中: {args.pattern!r}。")
        print("  换同义词/近义词再试；扫描版先确认已经 OCR；"
              "也可用 outline --chapters 看看章节名。")
        return 1
    print(f"命中 {hits} 处，分布在 {len(printed_pages)} 页："
          f"{sorted(printed_pages)[:20]}{' …' if len(printed_pages) > 20 else ''}")
    print("下一步：用 text --pages 读命中页附近的正文，或 render 出图视觉核对。")
    return 0


def cmd_render(args):
    doc = C.open_doc(args.pdf, password=args.password)
    pages = C.parse_pages(args.pages, doc.page_count, offset=args.offset)
    if not pages:
        C.die("页范围为空（扫描版页码换算用 --offset）")
    out_dir = Path(C.norm_path(args.out))
    out_dir.mkdir(parents=True, exist_ok=True)
    pymupdf = C.import_pymupdf()
    for n in pages:
        page = doc[n - 1]
        dpi = args.dpi
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        note = ""
        if pix.width > args.max_width:
            # 按目标像素宽度反推 dpi（不是把像素宽度乘个系数——那会算出 40dpi 的糊图）
            dpi = max(36, int(args.dpi * args.max_width / pix.width))
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            note = f"（受 --max-width {args.max_width} 限制，实际 {dpi}dpi）"
        path = out_dir / f"p{n:04d}.png"
        pix.save(str(path))
        print(f"  {path.name}  {pix.width}x{pix.height}  "
              f"{path.stat().st_size / 1024:.0f}KB{note}")
    print(f"已渲染 {len(pages)} 页 → {out_dir}")
    print("下一步：用 Read 工具逐张看这些 PNG，只记录你真正看到的内容。")
    doc.close()
    return 0


# ==========================================================================
# 子命令：split / merge / pages
# ==========================================================================
def page_weight(doc, page_no: int) -> int:
    """粗略估计一页占多少字节（内容流 + 引用的图片对象）。"""
    page = doc[page_no - 1]
    total = 0
    try:
        for xref in page.get_contents():
            total += len(doc.xref_stream_raw(xref) or b"")
    except Exception:
        pass
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                total += len(doc.xref_stream_raw(xref) or b"")
            except Exception:
                total += 200_000
    except Exception:
        pass
    return max(total, 4096)


def cmd_split(args):
    src = C.ensure_pdf(args.pdf)
    doc = C.open_doc(src, password=args.password)
    page_count = doc.page_count
    stem = C.slugify(Path(src).stem, 30)
    out_dir = Path(C.norm_path(args.out))
    out_dir.mkdir(parents=True, exist_ok=True)

    titles: dict[int, str] = {}
    ranges: list[tuple[int, int]] = []
    how = ""
    if args.by_bookmarks:
        toc = doc.get_toc(simple=True)
        degenerate, ratio = outline_is_degenerate(toc)
        if not toc or degenerate:
            doc.close()
            C.die("这本书的书签不适合用来拆分（没有书签，或 60% 以上是扫描占位名）。"
                  "改用 --every N 或 --ranges 手工指定页范围。")
        entries = [(lvl, str(t).strip(), p) for lvl, t, p in toc
                   if lvl <= args.level and 1 <= p <= page_count]
        starts = sorted({p for _l, _t, p in entries})
        first_titles = {}
        for lvl, t, p in entries:
            first_titles.setdefault(p, t)
        for i, start in enumerate(starts):
            end = starts[i + 1] - 1 if i + 1 < len(starts) else page_count
            ranges.append((start, end))
            titles[start] = first_titles.get(start, f"p.{start}")
        how = f"按书签（顶层 {len(starts)} 段）"
    elif args.every:
        ranges = [(i, min(i + args.every - 1, page_count))
                  for i in range(1, page_count + 1, args.every)]
        how = f"每 {args.every} 页一份"
    elif args.size:
        weights = {p: page_weight(doc, p) for p in range(1, page_count + 1)}
        target = int(args.size * 1048576)
        ranges = C.greedy_chunks([(1, page_count)], weights, target)
        how = f"按约 {args.size} MB 切"
    else:
        ranges = C.parse_ranges(args.ranges, page_count)
        how = "按给定页范围"
        # 安全网：页范围是用户手写的，写漏一段就可能把整页书悄悄丢掉。
        # 明确报出没被任何区间覆盖的页，而不是安静地少切几页。
        covered: set[int] = set()
        for a, b in ranges:
            covered.update(range(a, b + 1))
        missing = [p for p in range(1, page_count + 1) if p not in covered]
        if missing:
            shown = ", ".join(f"p.{p}" for p in missing[:20])
            more = f" 等 {len(missing)} 页" if len(missing) > 20 else ""
            print(f"⚠ 有 {len(missing)} 页不在任何页范围里，不会出现在任何分册中：{shown}{more}")
            print("  （如果这不是你要的，补全 --ranges；要按固定页数切改用 --every N）")
        # 重叠区间会让同一页出现在两个分册里，也提示一下
        seen: set[int] = set()
        overlaps: set[int] = set()
        for a, b in ranges:
            for p in range(a, b + 1):
                if p in seen:
                    overlaps.add(p)
                seen.add(p)
        if overlaps:
            shown = ", ".join(f"p.{p}" for p in sorted(overlaps)[:20])
            print(f"⚠ 有 {len(overlaps)} 页同时落在多个页范围里（会重复出现在多个分册中）：{shown}")

    if args.limit and len(ranges) > args.limit:
        print(f"⚠ 会切出 {len(ranges)} 个文件，只做前 {args.limit} 个"
              "（要全做请调高 --limit）")
        ranges = ranges[:args.limit]

    print(f"拆分方式：{how}；共 {page_count} 页 → {len(ranges)} 个文件")
    manifest = []
    for (a, b) in ranges:
        title = titles.get(a)
        name = (f"{args.prefix or stem}_p{a:03d}-{b:03d}.pdf"
                if not title else
                f"{args.prefix or stem}_{C.slugify(title, 24)}_p{a:03d}-{b:03d}.pdf")
        target = C.unique_path(out_dir / name) if not args.force else out_dir / name
        part = C.open_doc(src, password=args.password)
        try:
            part.select(list(range(a - 1, b)))
            part.save(str(target), garbage=3, deflate=True)
        except Exception as exc:
            C.warn(f"第 {a}-{b} 页写出失败：{exc}")
            part.close()
            continue
        size = target.stat().st_size
        part.close()
        manifest.append({"file": str(target), "from": a, "to": b,
                         "pages": b - a + 1, "title": title,
                         "size_mb": round(size / 1048576, 2)})
        print(f"  {target.name}  p.{a}-{b}（{b - a + 1} 页，"
              f"{manifest[-1]['size_mb']} MB）")
    doc.close()
    man_path = out_dir / "_split_manifest.json"
    man_path.write_text(json.dumps(
        {"source": src, "how": how, "parts": manifest},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {len(manifest)} 个 PDF + 清单 → {man_path}")
    return 0


def cmd_merge(args):
    files = [C.ensure_pdf(f) for f in args.files]
    if len(files) < 1:
        C.die("至少要给一个输入文件")
    out_path = Path(C.norm_path(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.force:
        out_path = C.unique_path(out_path)
    pymupdf = C.import_pymupdf()
    out = pymupdf.open()
    toc: list = []
    offset = 0
    plan = []
    for f in files:
        src_doc = C.open_doc(f, password=args.password)
        n = src_doc.page_count
        out.insert_pdf(src_doc)
        entries = src_doc.get_toc(simple=True)
        degenerate, _ = outline_is_degenerate(entries)
        if args.toc_title:
            # 每个来源文件都挂一条顶层书签，不管它自己有没有目录
            toc.append([1, Path(f).stem, offset + 1])
        if entries and not degenerate:
            sub = [(lvl, t, p) for lvl, t, p in entries if lvl <= args.level]
            # --toc-title 只给每个来源加一条顶层书签，**不该顺手把源书签整体压深一级**：
            # 以前两者一起做（base=1），结果一本原本"章=1级"的书合并后变成"章=2级"，
            # 层级整体下沉、和原书对不上。要嵌套层级请显式加 --nest-toc。
            base = 1 if (args.toc_title and args.nest_toc) else 0
            for lvl, t, p in sub:
                toc.append([min(lvl + base, 6), str(t).strip(), offset + p])
            bookmark_note = len(sub)
        else:
            bookmark_note = 0
        plan.append({"file": f, "pages": n, "start": offset + 1,
                     "end": offset + n, "bookmarks": len(entries),
                     "bookmarks_used": bookmark_note})
        offset += n
        src_doc.close()
    if args.toc:
        custom = json.loads(Path(C.norm_path(args.toc)).read_text(encoding="utf-8"))
        toc = custom.get("toc", custom) if isinstance(custom, dict) else custom
    if toc and not args.no_toc:
        try:
            out.set_toc([[int(a), str(b), int(c)] for a, b, c in toc])
        except Exception as exc:
            C.warn(f"写入书签失败（PDF 继续生成）：{exc}")
    if args.title or args.author:
        md = out.metadata or {}
        if args.title:
            md["title"] = args.title
        if args.author:
            md["author"] = args.author
        try:
            out.set_metadata(md)
        except Exception:
            pass
    out.save(str(out_path), garbage=3, deflate=True)
    out.close()
    print(f"已合并 {len(files)} 个文件 → {out_path}（{offset} 页）")
    for p in plan:
        print(f"  {Path(p['file']).name}：p.{p['start']}-p.{p['end']}"
              f"（{p['pages']} 页，书签 {p['bookmarks']} 条）")
    if toc and not args.no_toc:
        print(f"书签：{len(toc)} 条已写入（--toc 可自定义，--no-toc 可关闭）")
    elif not toc and not args.no_toc:
        print("提示：这次没写出任何目录（来源文件没有可用书签，也没加 --toc-title）。"
              "需要目录就加 --toc-title 按文件建顶层条目，或用 --toc <json> 手工给一份。")
    return 0


def cmd_pages(args):
    src = C.ensure_pdf(args.pdf)
    doc = C.open_doc(src, password=args.password)
    page_count = doc.page_count
    keep = set(range(1, page_count + 1))
    if args.select:
        keep = set(C.parse_pages(args.select, page_count))
    if args.delete:
        keep -= set(C.parse_pages(args.delete, page_count))
    if args.reverse:
        order = sorted(keep, reverse=True)
    else:
        order = sorted(keep)
    if not order:
        doc.close()
        C.die("选择/删除之后一页都不剩了")
    out_path = Path(C.norm_path(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.force:
        out_path = C.unique_path(out_path)

    changed = bool(args.select or args.delete or args.rotate or args.rotate_all
                   or args.reverse)
    new = C.open_doc(src, password=args.password)
    if args.reverse:
        # select() 只接受升序，倒序用 insert_pdf 逐页反向拼
        old = C.open_doc(src, password=args.password)
        new = C.import_pymupdf().open()
        for p in reversed(sorted(keep)):
            new.insert_pdf(old, from_page=p - 1, to_page=p - 1)
        old.close()
    elif len(order) != page_count:
        new.select([p - 1 for p in order])
    if args.rotate_all:
        for page in new:
            page.set_rotation((page.rotation + args.rotate_all) % 360)
    for spec in args.rotate or []:
        m = re.match(r"^(\d+(?:-\d+)?)\s*[:=]\s*(-?\d+)$", spec.strip())
        if not m:
            new.close()
            C.die(f"旋转参数看不懂: {spec!r}（形如 3:90 或 5-8:180）")
        targets = C.parse_pages(m.group(1), new.page_count)
        angle = int(m.group(2))
        for t in targets:
            page = new[t - 1]
            page.set_rotation((page.rotation + angle) % 360)
    new.save(str(out_path), garbage=3, deflate=True)
    new.close()
    doc.close()
    print(f"已写出 → {out_path}")
    print(f"  保留 {len(order)} 页（原 {page_count} 页）"
          + (f"；旋转 --rotate {args.rotate}" if args.rotate or args.rotate_all else ""))
    if not changed:
        print("  （没有指定任何改动：这次相当于另存一份，顺带做了压缩清理）")
    if args.select or args.delete:
        span = f"{order[0]}-{order[-1]}" if order == list(range(order[0], order[-1] + 1)) \
            else f"{len(order)} 个不连续页"
        print(f"  页范围：{span}")
    return 0


# ==========================================================================
# 子命令：forms / outline
# ==========================================================================
def field_rows(doc, pages: list[int] | None = None) -> list[dict]:
    rows = []
    rng = pages or range(1, doc.page_count + 1)
    for n in rng:
        for w in (doc[n - 1].widgets() or []):
            rows.append({
                "page": n,
                "name": w.field_name,
                "type": getattr(w, "field_type_string", "?"),
                "value": w.field_value if isinstance(w.field_value, str)
                else ("" if w.field_value is None else str(w.field_value)),
                "options": list(getattr(w, "choice_values", None) or []) or None,
                "label": getattr(w, "field_label", "") or "",
                "readonly": bool(getattr(w, "field_flags", 0) & 1)
                if getattr(w, "field_flags", None) is not None else False,
                "states": list(getattr(w, "button_states", lambda: {})() or {})
                if getattr(w, "button_states", None) else [],
            })
    return rows


def cmd_forms(args):
    doc = C.open_doc(args.pdf, password=args.password)
    xfa = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0] not in ("null", "none", None)
    if not getattr(doc, "is_form_pdf", 0):
        if not xfa:
            print("这个 PDF 里没有表单字段（AcroForm 为空）。")
            print("  如果页面上看着有填空线，那多半是画出来的线条而不是表单域，"
                  "不能用 forms --fill 填；需要直接在页面上写字才行。")
            doc.close()
            return 1
    rows = field_rows(doc)
    if xfa:
        print("⚠ 这是 XFA 表单：字段能列出来，但 PyMuPDF 无法改写其内容。"
              "填写请用 Adobe Acrobat / Foxit 等支持 XFA 的工具。")
    if args.export or args.template:
        payload = {r["name"]: r["value"] for r in rows}
        target = args.template or args.export
        path = Path(C.norm_path(target))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"已写出字段 JSON → {path}"
              + ("（把值填进去后用 --fill 回填）" if args.template else ""))
    if args.reset:
        for n in range(1, doc.page_count + 1):
            for w in (doc[n - 1].widgets() or []):
                try:
                    w.reset()
                    w.update()
                except Exception:
                    pass
        print("已清空所有字段（--reset）")
    values: dict = {}
    if args.fill:
        data = json.loads(Path(C.norm_path(args.fill)).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            C.die("--fill 的 JSON 必须是 {字段名: 值} 的字典")
        values.update(data)
    for item in args.set or []:
        if "=" not in item:
            C.die(f"--set 需要 名字=值 的形式：{item!r}")
        k, v = item.split("=", 1)
        values[k.strip()] = v.strip()
    filled, unknown = 0, []
    if values:
        # 必须在"遍历该页 widget 生成器"的过程中就地赋值：PyMuPDF 的 Widget
        # 一旦离开生成器（存到变量里晚点再改）就会报 "Annot is not bound to a page"。
        wanted = dict(values)
        for n in range(1, doc.page_count + 1):
            for w in (doc[n - 1].widgets() or []):
                name = w.field_name
                if name not in wanted:
                    continue
                raw_value = wanted.pop(name)
                kind = str(getattr(w, "field_type_string", "") or "").lower()
                value = raw_value
                if "check" in kind or "radio" in kind:
                    if isinstance(raw_value, bool):
                        value = "Yes" if raw_value else "Off"
                    else:
                        text = str(raw_value).strip().lower()
                        value = "Yes" if text in ("1", "true", "yes", "on", "是", "y") else "Off"
                else:
                    choices = list(getattr(w, "choice_values", None) or [])
                    if choices and str(value) not in choices:
                        near = [c for c in choices if str(c).lower() == str(value).lower()]
                        if near:
                            value = near[0]
                        else:
                            C.warn(f"字段 {name} 的值 {value!r} 不在选项里 {choices}，"
                                   "仍然照填（可能显示为空）")
                    value = "" if value is None else str(value)
                try:
                    w.field_value = value
                    w.update()
                    filled += 1
                except Exception as exc:
                    C.warn(f"字段 {name} 写不进去：{exc}")
        unknown = list(wanted)
        if unknown:
            C.warn(f"这些字段名在 PDF 里不存在，已跳过：{unknown}")
            print(f"  可用字段名：{[r['name'] for r in rows][:20]}")
    if args.flatten:
        try:
            doc.bake(widgets=True, annots=bool(args.flatten_annots))
            print("已扁平化表单（字段变成普通页面内容，不能再编辑）")
        except Exception as exc:
            C.warn(f"扁平化失败：{exc}")
    if args.out:
        out_path = Path(C.norm_path(args.out))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path), garbage=3, deflate=True)
        print(f"已写出 → {out_path}（填了 {filled} 个字段）")
        if values:
            # 回显必须来自**已写出的文件**：rows 是填写之前读的，
            # 直接拿它打印会把旧值当成结果给用户看（磁盘上其实已经写对了）。
            try:
                with C.open_doc(str(out_path)) as check_doc:
                    saved_rows = field_rows(check_doc)
                if saved_rows:
                    rows = saved_rows
            except Exception as exc:
                C.warn(f"回读已写出的文件核对字段值失败：{exc}")
    elif values:
        print(f"⚠ 填了 {filled} 个字段但没有 --out，改动没有保存。"
              "加 --out <输出.pdf> 才会落盘。")
    if not args.quiet and rows:
        print(f"共 {len(rows)} 个字段：")
        for r in rows:
            extra = f"｜选项 {r['options']}" if r["options"] else ""
            print(f"  p.{r['page']:<4} {r['name']:<24} {r['type']:<10} "
                  f"值={r['value']!r}{extra}")
    elif not rows:
        print("没有可列出的表单字段。")
    doc.close()
    return 0


def cmd_outline(args):
    doc = C.open_doc(args.pdf, password=args.password)
    toc = doc.get_toc(simple=True)
    if args.set:
        # 这是本工具里唯一会抛原始 traceback 的路径：文件不存在 / JSON 坏掉
        # 都会直接冒到用户面前，与其他子命令"一行中文提示 + exit 2"不一致。
        set_path = Path(C.norm_path(args.set))
        if not set_path.exists():
            C.die(f"--set 指定的 JSON 不存在：{set_path}\n"
                  "  先用 outline --json 导出一份，改好再用 --set 写回。")
        try:
            data = json.loads(set_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            C.die(f"--set 的 JSON 解析不了：{set_path}\n"
                  f"  第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}\n"
                  "  期望格式：[[1,\"第一章\",1],[2,\"小节\",3]] 或 {\"toc\": [[…]]}")
        if isinstance(data, dict):
            data = data.get("toc", [])
        toc_new = [[int(a), str(b), int(c)] for a, b, c in data]
        doc.set_toc(toc_new)
        out_path = Path(C.norm_path(args.out or args.pdf))
        if args.out:
            doc.save(str(out_path), garbage=3, deflate=True)
            print(f"已写入 {len(toc_new)} 条书签 → {out_path}")
        else:
            C.die("--set 需要同时用 --out 指定输出文件（不覆盖原文件）")
        doc.close()
        return 0
    if not toc:
        if args.json:
            print(json.dumps({"bookmarks": [], "degenerate": True}, ensure_ascii=False))
        else:
            print("（这本书没有书签大纲。）")
            print("扫描版可以：OCR 后从正文标题推断结构（to-ai 会做），"
                  "或视觉阅读印刷目录页。")
        doc.close()
        return 1
    degenerate, ratio = outline_is_degenerate(toc)
    payload = {"file": C.norm_path(args.pdf), "entries": len(toc),
               "degenerate": degenerate, "junk_ratio": round(ratio, 3),
               "bookmarks": [{"level": lvl, "title": str(t).strip(), "page": p}
                             for lvl, t, p in toc if lvl <= args.max_level]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if degenerate:
            print(f"⚠ 这份书签有 {ratio:.0%} 是扫描流水线生成的占位名"
                  "（如 fow001 / 000123），不能当目录用。")
            print(f"  样例：{'、'.join(str(e[1]).strip() for e in toc[:6])}")
            print("  → 改走视觉路线取结构，或 OCR 后从正文标题推断（to-ai 会做）。")
            if not args.force:
                doc.close()
                return 1
        if args.chapters:
            cmap = chapter_map(toc, doc.page_count, args.level)
            print(f"共 {doc.page_count} 页；顶层章节 {len(cmap)} 个")
            for c in cmap:
                print(f"  {c['start']:>5}-{c['end']:<5} ({c['pages']:>4})  {c['title']}")
        elif args.md:
            for lvl, t, p in toc:
                if lvl <= args.max_level:
                    print(f"{'  ' * (lvl - 1)}- {str(t).strip()}（p.{p}）")
        else:
            for lvl, t, p in toc:
                if lvl <= args.max_level:
                    print(f"{'  ' * (lvl - 1)}{p:>6}  {str(t).strip()}")
    doc.close()
    return 0


# ==========================================================================
# CLI
# ==========================================================================
def build_parser():
    ap = argparse.ArgumentParser(
        prog="pdftool.py",
        description="PDF 工具箱：AI 友好化 / OCR / 正文与表格 / 拆分合并 / 表单 / 图片")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--password", help="加密 PDF 的口令")

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, parents=[common])
        p.set_defaults(func=fn)
        return p

    add("check", cmd_check, "环境自检：PyMuPDF 与 OCR 引擎可用性")

    p = add("info", cmd_info, "看这本书：页数/文字层/书签/表单/图片 + 下一步建议")
    p.add_argument("pdf")
    p.add_argument("--method", choices=["auto", "pymupdf", "pdftotext"], default="auto")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--out", help="把报告写成 JSON 文件")

    p = add("text", cmd_text, "抽正文（带 [[p.N]] 页锚）")
    p.add_argument("pdf")
    p.add_argument("--pages", help="页范围，如 5-12 或 3,9,20（默认前 12 页）")
    p.add_argument("--offset", type=int, default=0, help="印刷页码 + offset = PDF 页号")
    p.add_argument("--mode", choices=["raw", "blocks", "markdown"], default="raw",
                   help="raw=原样抽取；blocks=按块排序；markdown=带标题/表格/插图")
    p.add_argument("--tables", action="store_true", help="markdown 模式下识别表格")
    p.add_argument("-o", "--out", help="写到文件（默认打印）")
    p.add_argument("--default-pages", type=int, default=12)

    p = add("to-ai", cmd_to_ai, "打成 AI 友好包：Markdown + 页锚全文 + manifest")
    p.add_argument("pdf")
    p.add_argument("-o", "--out", help="输出目录（默认 <文件名>_ai）")
    p.add_argument("--title", help="书名/标题（默认取文件名）")
    p.add_argument("--pages", help="只打包这些页")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--method", choices=["auto", "pymupdf", "pdftotext"], default="auto")
    p.add_argument("--images", action="store_true", help="导出插图")
    p.add_argument("--tables", action="store_true", help="识别并导出表格（CSV/Markdown）")
    p.add_argument("--table-format", choices=["csv", "md", "all"], default="csv")
    p.add_argument("--table-strategy", choices=["lines", "text"], help="find_tables 策略")
    p.add_argument("--min-rows", type=int, default=2)
    p.add_argument("--min-cols", type=int, default=2)
    p.add_argument("--min-image-kb", type=int, default=8, help="小于这个大小的图不导出")
    p.add_argument("--min-image-px", type=int, default=40)
    p.add_argument("--page-scans-are-figures", action="store_true",
                   help="把整页扫描底图也算作插图（默认不算）")
    p.add_argument("--ocr", action="store_true", help="先把扫描页 OCR 出文字层再打包")
    p.add_argument("--engine", default="auto", help="OCR 引擎 auto/rapidocr/tesseract")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 4) // 3)))
    p.add_argument("--min-conf", type=float, default=0.5)
    p.add_argument("--keep-headers", action="store_true", help="保留页眉页脚")
    p.add_argument("--no-pages-txt", action="store_true", help="不写 .pages.txt")

    p = add("ocr", cmd_ocr, "给扫描版加可搜索文字层（另可选导出页锚全文）")
    p.add_argument("pdf")
    p.add_argument("-o", "--out", help="输出 PDF（默认 <文件名>_ocr.pdf）")
    p.add_argument("--pages", help="只做这些页（默认全书）")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--engine", default="auto", help="auto / rapidocr / tesseract")
    p.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 4) // 3)))
    p.add_argument("--min-conf", type=float, default=0.5, help="低于此置信度的行不写入")
    p.add_argument("--existing", choices=["auto", "force", "skip"], default="auto",
                   help="auto=只做没有文字层/乱码的页；force=全部重做；skip=只做完全没文字的页")
    p.add_argument("--sidecar", help="另写整册 [[p.N]] 页锚全文（给 verify.py 当 --source）")
    p.add_argument("--no-pdf", action="store_true", help="不写 PDF，只要文本")
    p.add_argument("--dry-run", action="store_true", help="只列出会做哪些页")
    p.add_argument("--force", action="store_true", help="允许覆盖同名输出")
    p.add_argument("--json", action="store_true")

    p = add("tables", cmd_tables, "抽表格 → CSV/Markdown/JSON")
    p.add_argument("pdf")
    p.add_argument("--pages", help="页范围（默认全书）")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("-o", "--out", help="输出目录（默认只打印，不落盘）")
    p.add_argument("--format", choices=["csv", "md", "all"], default="csv")
    p.add_argument("--table-strategy", choices=["lines", "text"], help="find_tables 策略")
    p.add_argument("--min-rows", type=int, default=2)
    p.add_argument("--min-cols", type=int, default=2)
    p.add_argument("--print", action="store_true", help="把表格以 Markdown 打印出来")
    p.add_argument("--json", action="store_true")

    p = add("images", cmd_images, "提取内嵌图片；--render-pages 则是整页出图")
    p.add_argument("pdf")
    # 不填 --out 时落到 <文件名>_images/：文档里多处把它写成可选
    # （如 interop-book-kb.md 的 "images --render-pages --dpi 200"），
    # 但 CLI 一直要求必填，照文档敲会直接报缺参数。
    p.add_argument("-o", "--out", default=None,
                   help="输出目录（默认 <文件名>_images）")
    p.add_argument("--pages", help="页范围（默认全书）")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--render-pages", action="store_true", help="渲染整页为 PNG")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--min-kb", type=int, default=8, help="小于这个大小的图不要")
    p.add_argument("--min-px", type=int, default=64, help="宽或高小于这个像素的不要")
    p.add_argument("--dedupe", action="store_true", help="内容相同的图只留一份")
    p.add_argument("--skip-page-scans", action="store_true", help="跳过整页扫描底图")
    p.add_argument("--list", action="store_true", help="以 JSON 列出（不写文件）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--show", type=int, default=25, help="最多打印多少条")

    p = add("search", cmd_search, "在文字层里检索关键词，返回页号 + 上下文")
    p.add_argument("pdf")
    p.add_argument("--pattern", required=True, help="要搜的词（默认按纯文本匹配）")
    p.add_argument("--pages", help="限定页范围，如 100-200")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--context", type=int, default=3, help="上下文字行数（默认 3）")
    p.add_argument("--max-hits", type=int, default=20)
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--regex", action="store_true", help="把 pattern 当正则")
    p.add_argument("--method", choices=["auto", "pymupdf", "pdftotext"], default="auto")
    p.add_argument("--force", action="store_true", help="扫描版也强行搜（通常搜不到）")

    p = add("render", cmd_render, "渲染指定页为 PNG（视觉阅读用）")
    p.add_argument("pdf")
    p.add_argument("--pages", help="页范围，如 5,9-12（默认前 12 页）")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--max-width", type=int, default=1500)
    p.add_argument("-o", "--out", default="pdf_pages", help="输出目录（默认 pdf_pages）")

    p = add("split", cmd_split, "拆分 PDF（按页数/页范围/书签/大小）")
    p.add_argument("pdf")
    p.add_argument("-o", "--out", required=True, help="输出目录")
    p.add_argument("--every", type=int, help="每 N 页一个文件")
    p.add_argument("--ranges", help="页范围清单，如 1-20,21-45")
    p.add_argument("--by-bookmarks", action="store_true", help="按顶层书签切")
    p.add_argument("--level", type=int, default=1, help="按书签切时的层级（默认 1）")
    p.add_argument("--size", type=float, help="按约 N MB 一个文件切")
    p.add_argument("--prefix", help="文件名前缀（默认取原文件名）")
    p.add_argument("--limit", type=int, default=100, help="最多切几个文件（防手滑）")
    p.add_argument("--force", action="store_true", help="允许覆盖同名文件")

    p = add("merge", cmd_merge, "合并 PDF（可选带书签）")
    p.add_argument("files", nargs="+", help="按顺序合并的文件")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--toc", help="自定义书签 JSON：[[级别, 标题, 页码], ...]")
    p.add_argument("--toc-title", action="store_true", help="给每个来源文件加一条顶层书签")
    p.add_argument("--nest-toc", action="store_true",
                   help="把来源书签整体降一级挂到 --toc-title 下面（默认保持原层级）")
    p.add_argument("--level", type=int, default=3, help="沿用来源书签的层级上限")
    p.add_argument("--no-toc", action="store_true")
    p.add_argument("--title", help="写入 PDF 元数据标题")
    p.add_argument("--author")
    p.add_argument("--force", action="store_true", help="允许覆盖同名输出")

    p = add("pages", cmd_pages, "页面级手术：选取/删除/旋转/倒序")
    p.add_argument("pdf")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--select", help="只保留这些页，如 1-20,30")
    p.add_argument("--delete", help="删掉这些页")
    p.add_argument("--rotate", action="append", help="形如 3:90 或 5-8:180（可多次）")
    p.add_argument("--rotate-all", type=int, help="整册顺时针旋转角度")
    p.add_argument("--reverse", action="store_true", help="页序倒过来")
    p.add_argument("--force", action="store_true")

    p = add("forms", cmd_forms, "看/填/扁平化 PDF 表单")
    p.add_argument("pdf")
    p.add_argument("--list", action="store_true", help="列出字段（默认就列）")
    p.add_argument("--template", help="导出填写模板 JSON（字段名→空值）")
    p.add_argument("--export", help="导出当前字段值 JSON")
    p.add_argument("--fill", help="按 JSON 回填（{字段名: 值}）")
    p.add_argument("--set", action="append", help="单个字段赋值 名字=值（可多次）")
    p.add_argument("--reset", action="store_true", help="清空所有字段")
    p.add_argument("--flatten", action="store_true", help="扁平化（字段变普通内容）")
    p.add_argument("--flatten-annots", action="store_true", help="连注释一起扁平化")
    p.add_argument("-o", "--out", help="输出 PDF（不填则只查看）")
    p.add_argument("--quiet", action="store_true")

    p = add("outline", cmd_outline, "读书签大纲 / 导出 / 写入自定义书签")
    p.add_argument("pdf")
    p.add_argument("--chapters", action="store_true", help="只列顶层章节 + 页范围")
    p.add_argument("--md", action="store_true", help="输出成嵌套列表")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-level", type=int, default=3)
    p.add_argument("--level", type=int, default=2, help="--chapters 用的层级")
    p.add_argument("--set", help="按 JSON 写入书签（[[级别,标题,页码],...]）")
    p.add_argument("-o", "--out", help="--set 时的输出文件")
    p.add_argument("--force", action="store_true", help="占位书签也照打")

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        C.die("用户中断", 130)
        return 130


if __name__ == "__main__":
    sys.exit(main())
