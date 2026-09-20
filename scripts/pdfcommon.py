#!/usr/bin/env python3
"""pdf-toolkit 公共工具：路径规整、页范围解析、文字层判定、Markdown 拼装。

为什么单独一个模块：pdftool.py（文档操作）和 ocrtool.py（OCR）都要用同一套
"这本书有没有文字层"的判断。文字层判定的阈值刻意与 book-kb-builder 的
booktool.py 保持一致（同样四个标签 text-rich / text-partial / text-garbled /
scanned），这样两个 skill 对同一本书不会给出互相矛盾的结论：
pdf-toolkit 判定 scanned 的书，OCR 之后 booktool.py probe 应该判定 text-rich。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

# Windows 控制台默认可能是 GBK，中文输出会炸；统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 页锚：与 book-kb-builder 完全一致，N = PDF 物理页号（1 起）。
PAGE_MARK = "[[p.{}]]"
PAGE_MARK_RE = re.compile(r"\[\[p\.(\d+)\]\]")
VERDICT_LABELS = ("text-rich", "text-partial", "text-garbled", "scanned")

# 判断"抽出的是不是人话"用的高频词：乱码文本几乎不会命中这些词。
ZH_STOPWORDS = "的了是在和与不为有这我你他她它们个中上下"
EN_STOPWORDS = ("the", "and", "of", "to", "in", "is", "are", "that", "for", "with")

PDF_MAGIC = b"%PDF"


# --------------------------------------------------------------------------
# 输出与错误
# --------------------------------------------------------------------------
def die(msg: str, code: int = 2):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"⚠ {msg}", file=sys.stderr)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def norm_path(p: str) -> str:
    """把 Git-Bash 风格的 /c/Users/x 转成 Windows 能认的 C:/Users/x。

    模型常从 shell 拿到 msys 路径再交给 Windows Python，不做这一步会得到
    "文件不存在"这种极具误导性的报错。
    """
    p = str(p).strip().strip('"').strip("'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    if p.startswith("~"):
        return os.path.expanduser(p)
    return os.path.normpath(p) if p else p


def slugify(name: str, maxlen: int = 40) -> str:
    name = unicodedata.normalize("NFKC", str(name))
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", "", name).strip("._")
    return name[:maxlen] or "book"


def unique_path(path: Path) -> Path:
    """避免覆盖已有文件：a.pdf 存在就退到 a_1.pdf。"""
    if not path.exists():
        return path
    stem, suf, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        cand = parent / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
    return path


def ensure_pdf(path: str) -> str:
    """确认这是个 PDF 文件，并把常见"拿错文件"的情况说清楚。"""
    p = norm_path(path)
    if not os.path.exists(p):
        die(f"找不到文件: {p}")
    if os.path.isdir(p):
        die(f"这是一个目录，不是 PDF 文件: {p}")
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        die(f"读不了这个文件: {exc}")
        return p
    if head.startswith(PDF_MAGIC):
        return p
    if head.startswith(b"PK"):
        die(f"{Path(p).name} 不是 PDF（看起来是 EPUB/ZIP 压缩包）。"
            "EPUB 请交给 book-kb-builder skill 处理："
            "booktool.py 支持 epub，本工具只管 PDF。")
    if head[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
        die(f"{Path(p).name} 是图片，不是 PDF。要合成 PDF 请先转图，"
            "或把图片放进图片工具处理。")
    die(f"{Path(p).name} 不是 PDF 文件（开头字节 {head[:6]!r}）")
    return p


# --------------------------------------------------------------------------
# PyMuPDF / 文档打开
# --------------------------------------------------------------------------
def import_pymupdf():
    try:
        import pymupdf  # noqa
        return pymupdf
    except Exception:
        die("需要 PyMuPDF。用本 skill 自带的虚拟环境装：\n"
            f"  \"{sys.executable}\" -m pip install pymupdf")
    raise SystemExit(2)


def open_doc(path: str, password: str | None = None):
    """打开 PDF，并对加密/损坏给出可操作的提示。"""
    pymupdf = import_pymupdf()
    p = ensure_pdf(path)
    try:
        doc = pymupdf.open(p)
    except Exception as exc:
        die(f"打不开这个 PDF: {type(exc).__name__}: {exc}\n"
            "  常见原因：文件下载不完整、不是真正的 PDF、或用了不支持的加密。")
        raise SystemExit(2)
    if getattr(doc, "needs_pass", False):
        ok = False
        if password:
            try:
                ok = bool(doc.authenticate(password))
            except Exception:
                ok = False
        if not ok:
            doc.close()
            die(f"{Path(p).name} 有密码保护。请用 --password 提供口令"
                "（本工具不会去猜密码）。")
    if getattr(doc, "is_repaired", False):
        warn(f"{Path(p).name} 结构有损坏，已由 MuPDF 自动修复后打开；"
             "如果有页面缺失，请换一份完整文件。")
    return doc


def find_pdftotext() -> str | None:
    exe = shutil.which("pdftotext")
    if exe:
        return exe
    for cand in (
        r"C:\Program Files\Git\mingw64\bin\pdftotext.exe",
        r"C:\Program Files\Git\usr\bin\pdftotext.exe",
        r"C:\Program Files\poppler\Library\bin\pdftotext.exe",
        "/usr/bin/pdftotext",
        "/usr/local/bin/pdftotext",
    ):
        if os.path.exists(cand):
            return cand
    return None


# --------------------------------------------------------------------------
# 页范围解析
# --------------------------------------------------------------------------
def parse_pages(spec: str | None, page_count: int, offset: int = 0,
                default_n: int = 12) -> list[int]:
    """解析 '5,9-12' → 1-based PDF 物理页号列表（去重、保序、裁剪到书内）。

    offset 的语义：书上印的页码 + offset = PDF 物理页号（扫描版页码校准）。
    """
    if not spec:
        return list(range(1, min(page_count, default_n) + 1))
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-–~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            rng = range(min(a, b), max(a, b) + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            die(f"看不懂的页范围: {part!r}（形如 5 或 9-12，用英文逗号分隔）")
            return []
        for n in rng:
            pdf_page = n + offset
            if 1 <= pdf_page <= page_count:
                out.append(pdf_page)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def parse_ranges(spec: str | None, page_count: int) -> list[tuple[int, int]]:
    """解析成若干 (起, 止) 闭区间；区间按时间顺序合并，用于拆分。"""
    if not spec:
        return [(1, page_count)]
    pairs: list[tuple[int, int]] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-–~]\s*(\d+)$", part)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
        elif part.isdigit():
            a = b = int(part)
        else:
            die(f"看不懂的页范围: {part!r}（形如 1-20 或 5）")
            return []
        a, b = max(1, a), min(page_count, b)
        if a <= b:
            pairs.append((a, b))
    pairs.sort()
    merged: list[tuple[int, int]] = []
    for a, b in pairs:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged or [(1, page_count)]


def greedy_chunks(ranges: list[tuple[int, int]], weights: dict[int, int],
                  target_bytes: int) -> list[tuple[int, int]]:
    """按"每页大致占多少字节"把页区间切成接近目标大小的块。

    weights 来自各页内容流的字节数，只是估算——目的是让拆分结果别一个 2MB
    一个 80MB，而不是精确控制。
    """
    out: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_bytes = 0
    last_page = 0
    for a, b in ranges:
        for p in range(a, b + 1):
            w = weights.get(p, target_bytes // 20)
            if cur_start is not None and cur_bytes + w > target_bytes:
                out.append((cur_start, last_page))
                cur_start, cur_bytes = p, 0
            if cur_start is None:
                cur_start = p
            cur_bytes += w
            last_page = p
    if cur_start is not None:
        out.append((cur_start, last_page))
    return out


# --------------------------------------------------------------------------
# 文字层判定（与 book-kb-builder 的 booktool.py 同阈值）
# --------------------------------------------------------------------------
def score_text(text: str) -> dict:
    """给一段提取结果打分，用于在多个提取器之间择优，以及识别乱码。

    关键指标是"人话命中数"：真实中文文本必然包含大量高频虚词，而乱码
    （CID 未映射被当拉丁码位输出）虽然也是合法字符，却几乎不命中任何一个。
    """
    if not text:
        return {"chars": 0, "cjk": 0, "latin_words": 0, "stopword_hits": 0,
                "weird": 0, "score": 0.0, "cjk_ratio": 0.0}
    total = len(text)
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    latin_words = len(re.findall(r"[A-Za-z]{3,}", text))
    lower = text.lower()
    zh_hits = sum(text.count(w) for w in ZH_STOPWORDS)
    en_hits = sum(lower.count(" " + w + " ") for w in EN_STOPWORDS)
    weird = sum(1 for c in text
                if (0x80 <= ord(c) <= 0x2FF) or c in "{}<>|~^`\\")
    stopword_hits = zh_hits + en_hits
    score = (cjk * 2.0 + zh_hits * 8.0 + latin_words * 1.5
             + en_hits * 6.0 - weird * 1.5)
    return {"chars": total, "cjk": cjk, "latin_words": latin_words,
            "stopword_hits": stopword_hits, "weird": weird,
            "score": round(score, 1), "cjk_ratio": round(cjk / total, 4)}


def page_text_verdict(text: str) -> str:
    """单页判定：'' / 'ok' / 'garbled'（决定 OCR 时该不该重做这一页）。"""
    if len(text.strip()) < 15:
        return ""
    sc = score_text(text)
    if sc["chars"] < 100:
        return "ok"  # 样本太小不做乱码判定，宁可放过
    hits_per_1k = sc["stopword_hits"] / (sc["chars"] / 1000.0)
    return "ok" if hits_per_1k >= 1.0 else "garbled"


def assess_text(page_texts: list[str], sc: dict | None = None) -> dict:
    """判定整册文字层可用性，决定走文本路线还是视觉路线。

    text-rich    → 可 grep、可机械校验引文，走文本路线
    text-partial → 混合，正文靠读文本、图版靠视觉
    text-garbled → 有字符但不成话（字体缺映射），等于没有
    scanned      → 没有文字层，只能视觉阅读
    """
    pages = page_texts[1:]
    body = "\n".join(pages)
    n = max(len(pages), 1)
    score = sc or score_text(body)
    with_text = sum(1 for p in pages if len(p.strip()) > 50)
    ratio_with_text = with_text / n
    per_page = score["chars"] / n

    # 判定基准是**每页**密度而非总量：扫描件全书的产出约等于 0 字/页，
    # 而一本很薄的正经小册子总量虽小、每页却是满的。用总量会把后者误杀。
    if per_page < 15:
        verdict = "scanned"
    elif score["chars"] < 100:
        verdict = "text-partial"
    else:
        hits_per_1k = score["stopword_hits"] / (score["chars"] / 1000.0)
        if hits_per_1k < 1.0:
            verdict = "text-garbled"
        elif ratio_with_text < 0.70 or per_page < 300:
            verdict = "text-partial"
        else:
            verdict = "text-rich"

    return {
        "verdict": verdict,
        "chars": score["chars"],
        "cjk_chars": score["cjk"],
        "cjk_ratio": score["cjk_ratio"],
        "stopword_hits": score["stopword_hits"],
        "weird_chars": score["weird"],
        "per_page_chars": round(per_page, 1),
        "pages_with_text": with_text,
        "pages": n,
        "quality_score": score["score"],
    }


def extract_page_texts(doc, method: str = "auto",
                       pdf_path: str | None = None) -> tuple[list[str], str]:
    """抽每页文本（index 0 是占位空串），返回 (页文本, 用的方法)。

    两条路各有盲区，所以默认两条都试、取更像人话的那份：
      - pymupdf ：对中日韩 CID 字体稳，还能渲染页面（推荐首选）
      - pdftotext：极快，但中文字体缺 ToUnicode 映射时会输出乱码
    """
    order = [method] if method != "auto" else ["pymupdf", "pdftotext"]
    best_pages, best_method, best_score, tried = [""], "", None, {}
    for m in order:
        pages: list[str] = [""]
        try:
            if m == "pymupdf":
                pages = [""] + [p.get_text() for p in doc]
            elif m == "pdftotext":
                exe = find_pdftotext()
                if not exe or not pdf_path:
                    tried[m] = {"error": "pdftotext 未安装"}
                    continue
                r = subprocess.run([exe, "-layout", "-enc", "UTF-8",
                                    norm_path(pdf_path), "-"], capture_output=True)
                if r.returncode not in (0, 99):
                    tried[m] = {"error": f"pdftotext 退出码 {r.returncode}"}
                    continue
                parts = r.stdout.decode("utf-8", "replace").split("\f")
                if parts and not parts[-1].strip():
                    parts = parts[:-1]
                pages = [""] + parts
            else:
                die(f"未知提取器: {m}（可选 pymupdf / pdftotext）")
        except Exception as exc:
            tried[m] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        sc = score_text("\n".join(pages[1:]))
        tried[m] = sc
        if best_score is None or sc["score"] > best_score["score"]:
            best_pages, best_method, best_score = pages, m, sc
        # 已经足够好就不必再试另一种
        if method == "auto" and sc["stopword_hits"] > 0 and sc["chars"] > 2000:
            break
    return best_pages, best_method


# --------------------------------------------------------------------------
# 输出拼装
# --------------------------------------------------------------------------
def write_pages_txt(path: str | Path, pairs) -> Path:
    """写规范页锚全文：每条记录形如 [[p.N]] + 该页文本。

    格式与 book-kb-builder 的 booktool.py probe 产出**逐字节一致**，
    因此可以直接当 verify.py 的 --source 用。
    """
    out = Path(norm_path(str(path)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for page_no, text in pairs:
            fh.write(PAGE_MARK.format(page_no) + "\n")
            fh.write((text or "").rstrip() + "\n\n")
    return out


def md_escape_cell(text: str) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return re.sub(r"\s+", " ", s).strip()


def md_table(rows: list[list[str]], header: list[str] | None = None,
             align_right: bool = False) -> str:
    """把二维表拼成 Markdown 表格；列数按最宽的一行对齐。"""
    clean = [[md_escape_cell(c) for c in row] for row in rows if row]
    if not clean and not header:
        return ""
    width = max([len(r) for r in clean] + ([len(header)] if header else [0]))
    lines: list[str] = []
    if header:
        head = [md_escape_cell(c) for c in header] + [""] * (width - len(header))
        lines.append("| " + " | ".join(head) + " |")
        lines.append("| " + " | ".join(["---:"] * width if align_right
                                       else ["---"] * width) + " |")
    elif clean:
        lines.append("| " + " | ".join(clean[0]) + " |")
        lines.append("| " + " | ".join(["---"] * width) + " |")
        clean = clean[1:]
    for row in clean:
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def strip_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def compact(s: str) -> str:
    """去掉所有空白。中文提取结果里常被插入多余空格，检索前先压平。"""
    return re.sub(r"\s+", "", s)


def print_progress(done: int, total: int, note: str = "", every: int = 1):
    """单行进度；every>1 时只在每 N 页打印一次，避免刷屏。"""
    if every > 1 and done % every and done != total:
        return
    pct = 100.0 * done / max(total, 1)
    print(f"  [{done:>4}/{total}] {pct:5.1f}%  {note}")
