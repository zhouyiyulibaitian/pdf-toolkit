#!/usr/bin/env python3
"""OCR 引擎封装：给扫描版 PDF 写入可搜索的隐藏文字层。

产出的是"图像原样保留 + 不可见文字层"的 PDF（和 ocrmypdf 一个思路）：
视觉上还是原来的扫描件，但可以选中、复制、Ctrl+F 搜索、被文字提取器读到。
这样扫描版图书就能被 book-kb-builder 的文本路线处理，引文也能被机器核对。

引擎优先级（--engine auto）：
  1. rapidocr  —— pip 安装、自带 PP-OCRv6 中文模型、离线可用（本 skill 已装）
  2. tesseract —— 系统装了 tesseract + pytesseract 时可用
  3. ocrmypdf  —— 外部 CLI，整册流水线（去斜、分栏、压缩）质量最好，装了才用

为什么把文字写成"逐行隐藏文本"而不是简单铺一层：PDF 里文字的位置决定了
选中高亮、搜索命中的矩形落在哪里。逐行按 OCR 框放，并做水平缩放让文字宽度
贴合原文字宽，这样搜索"木作"时高亮的正是那一行字所在的位置。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdfcommon as C  # noqa: E402

# 低于这个置信度的行不写进文字层，免得把错字塞进可搜索文本、污染后续引文核对。
DEFAULT_MIN_CONF = 0.5
DEFAULT_DPI = 300


@dataclass
class OcrLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
class BaseEngine:
    name = "base"
    detail = ""

    def recognize(self, image) -> list[OcrLine]:
        raise NotImplementedError


class RapidOcrEngine(BaseEngine):
    """RapidOCR：ONNX 推理，自带中文识别模型，CPU 上约 3-5 秒/页（300dpi）。"""

    name = "rapidocr"

    def __init__(self, max_side_len: int = 4000, text_score: float = 0.5):
        self._impl = None
        self.detail = ""
        try:
            import rapidocr  # noqa
        except Exception:
            self.detail = "未安装（pip install rapidocr onnxruntime）"
            return
        params = {"Global.max_side_len": max_side_len,
                  "Global.text_score": text_score,
                  "Global.log_level": "error"}
        # v3：RapidOCR(params={...})，返回对象带 boxes/txts/scores
        try:
            from rapidocr import RapidOCR
            try:
                self._impl = RapidOCR(params=params)
            except Exception:
                self._impl = RapidOCR()
            self._call = self._call_v3
            return
        except Exception as exc:
            v3_err = f"{type(exc).__name__}: {exc}"
        # v2 兜底：rapidocr_onnxruntime，返回 (结果列表, 耗时)
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            self._impl = RapidOCR()
            self._call = self._call_v2
            return
        except Exception as exc:
            self.detail = f"导入失败：v3 {v3_err}；v2 {type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._impl is not None

    def _call_v3(self, image):
        res = self._impl(image)
        boxes, txts, scores = res.boxes, res.txts, res.scores
        if txts is None or boxes is None:
            return []
        if scores is None:
            scores = [1.0] * len(txts)
        out = []
        for box, txt, sc in zip(boxes, txts, scores):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            out.append(OcrLine(txt, min(xs), min(ys), max(xs), max(ys), float(sc)))
        return out

    def _call_v2(self, image):
        res, _elapse = self._impl(image)
        out = []
        for item in res or []:
            box, txt, sc = item[0], item[1], item[2]
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            out.append(OcrLine(txt, min(xs), min(ys), max(xs), max(ys), float(sc)))
        return out

    def recognize(self, image) -> list[OcrLine]:
        if self._impl is None:
            return []
        try:
            return self._call(image)
        except Exception as exc:
            C.warn(f"RapidOCR 这一页识别失败（该页不写文字层）："
                   f"{type(exc).__name__}: {exc}")
            return []


class TesseractEngine(BaseEngine):
    """系统 tesseract + pytesseract。中文需要 chi_sim 语言包。"""

    name = "tesseract"

    def __init__(self, lang: str = "chi_sim+eng"):
        self._lang = lang
        self._impl = None
        self.detail = ""
        try:
            import pytesseract
            from PIL import Image  # noqa
            self._impl = pytesseract
        except Exception as exc:
            self.detail = f"未安装 pytesseract/Pillow（{type(exc).__name__}）"
            return
        if not shutil.which("tesseract"):
            self.detail = "没找到 tesseract 可执行文件（装 Tesseract-OCR 并加入 PATH）"
            self._impl = None

    @property
    def available(self) -> bool:
        return self._impl is not None

    def recognize(self, image) -> list[OcrLine]:
        if self._impl is None:
            return []
        from PIL import Image
        from pytesseract import Output
        try:
            data = self._impl.image_to_data(Image.fromarray(image), lang=self._lang,
                                            output_type=Output.DICT)
        except Exception as exc:
            C.warn(f"tesseract 识别失败：{exc}")
            return []
        grouped: dict[tuple, list] = {}
        for i in range(len(data.get("text", []))):
            txt = (data["text"][i] or "").strip()
            if not txt:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            grouped.setdefault(key, []).append(
                (data["left"][i], data["top"][i], data["width"][i],
                 data["height"][i], txt, conf / 100.0))
        out: list[OcrLine] = []
        for _key, words in sorted(grouped.items()):
            out.append(OcrLine(
                " ".join(w[4] for w in words),
                float(min(w[0] for w in words)),
                float(min(w[1] for w in words)),
                float(max(w[0] + w[2] for w in words)),
                float(max(w[1] + w[3] for w in words)),
                sum(w[5] for w in words) / len(words)))
        return out


def rapidocr_status() -> tuple[bool, str]:
    """只检查"能不能用"，不真的加载模型。

    构造一次 RapidOCR 要加载三个 ONNX 模型（约 1-2 秒），只为报告可用性不值得；
    而且加载时会往 stdout 打一堆 INFO 日志，把 CLI 的输出冲得看不清。
    """
    try:
        import rapidocr
    except Exception as exc_v3:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True, "rapidocr_onnxruntime（v2 接口）"
        except Exception:
            return False, f"未安装（{type(exc_v3).__name__}）"
    root = Path(getattr(rapidocr, "__file__", "") or ".").parent / "models"
    try:
        models = sorted(root.glob("*.onnx"))
    except OSError:
        models = []
    if models:
        return True, f"自带 PP-OCRv6 中文模型（离线，{len(models)} 个模型文件）"
    return True, "已安装，但模型要首次联网下载"


def detect_engines() -> list[dict]:
    """报告每个引擎是否可用，供 `pdftool.py check` 与自动选择使用。"""
    rapid_ok, rapid_detail = rapidocr_status()
    tess = TesseractEngine()
    ocrmypdf = shutil.which("ocrmypdf")
    return [
        {"name": "rapidocr", "available": rapid_ok, "detail": rapid_detail},
        {"name": "tesseract", "available": tess.available,
         "detail": tess.detail or "系统 tesseract（chi_sim+eng）"},
        {"name": "ocrmypdf", "available": bool(ocrmypdf),
         "detail": ocrmypdf or "未安装（可选，需 Ghostscript；装了可用 --engine ocrmypdf）"},
    ]


def build_engine(name: str = "auto", **opts) -> BaseEngine:
    if name in ("auto", None):
        for cand in ("rapidocr", "tesseract"):
            eng = build_engine(cand, **opts)
            if getattr(eng, "available", False):
                return eng
        C.die("没有可用的 OCR 引擎。二选一：\n"
              f"  1) pip 装 RapidOCR（推荐，离线）：\"{sys.executable}\" -m pip install rapidocr onnxruntime pillow\n"
              "  2) 装系统 Tesseract-OCR + chi_sim 语言包，再 pip install pytesseract")
    if name == "rapidocr":
        return RapidOcrEngine(**{k: v for k, v in opts.items()
                                 if k in ("max_side_len", "text_score")})
    if name == "tesseract":
        return TesseractEngine(lang=opts.get("lang") or "chi_sim+eng")
    C.die(f"不认识的引擎: {name}（可选 auto / rapidocr / tesseract）")
    raise SystemExit(2)


# --------------------------------------------------------------------------
# 页面 → 图像 → 文字层
# --------------------------------------------------------------------------
def import_numpy():
    try:
        import numpy as np
        return np
    except Exception:
        C.die("OCR 需要 numpy/Pillow 等依赖。安装：\n"
              f"  \"{sys.executable}\" -m pip install rapidocr onnxruntime numpy pillow")
    raise SystemExit(2)


def pix_to_array(pix, np):
    """Pixmap → numpy 数组，处理 stride 与通道数。"""
    n = pix.n
    stride = pix.stride
    buf = np.frombuffer(pix.samples, dtype=np.uint8)
    if stride != pix.width * n:
        buf = buf.reshape(pix.height, stride)[:, : pix.width * n]
    arr = buf.reshape(pix.height, pix.width, n)
    if n == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif n == 4:
        arr = arr[:, :, :3]
    return arr


def render_array(page, dpi: int = DEFAULT_DPI):
    """渲染一页为 RGB numpy 数组（OCR 用；省掉 PNG 编解码）。"""
    np = import_numpy()
    pymupdf = C.import_pymupdf()
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
    return pix_to_array(pix, np)


def image_to_page_matrix(page, dpi: int):
    """图像像素坐标 → PDF 页面坐标的换算矩阵。

    页面带 /Rotate 时，渲染出来的位图是"旋转后"的样子，而文字必须写在
    未旋转的页面坐标系里，否则文字会落到页面外面。
    """
    pymupdf = C.import_pymupdf()
    scale = 72.0 / float(dpi)
    mat = pymupdf.Matrix(scale, 0, 0, scale, 0, 0)
    if getattr(page, "rotation", 0):
        mat = mat * page.derotation_matrix
    return mat


def insert_text_layer(page, lines: list[OcrLine], dpi: int = DEFAULT_DPI,
                      min_conf: float = DEFAULT_MIN_CONF,
                      fontname: str = "china-s") -> int:
    """把 OCR 行写成不可见文字层（render_mode=3），返回写入行数。

    逐行写 + 水平缩放，让文字宽度贴合 OCR 框宽度：这样选中/搜索的矩形
    落在纸质文字真正的位置上，而不是缩成一小团。
    """
    pymupdf = C.import_pymupdf()
    usable = [ln for ln in lines if ln.text.strip() and ln.conf >= min_conf]
    if not usable:
        return 0
    try:
        font = pymupdf.Font(fontname)
    except Exception:
        font = pymupdf.Font("helv")
    if not font.has_glyph(ord("中")):
        font = pymupdf.Font("china-s")
    try:
        page.insert_font(fontname="ocr-cjk", fontbuffer=font.buffer)
    except Exception:
        pass
    mat = image_to_page_matrix(page, dpi)
    written = 0
    for ln in usable:
        p0 = pymupdf.Point(ln.x0, ln.y0) * mat
        p1 = pymupdf.Point(ln.x1, ln.y1) * mat
        x0, x1 = min(p0.x, p1.x), max(p0.x, p1.x)
        y0, y1 = min(p0.y, p1.y), max(p0.y, p1.y)
        if x1 - x0 <= 0.5 or y1 - y0 <= 0.5:
            continue
        size = float(max(3.0, (y1 - y0) * 0.82))
        width = float(font.text_length(ln.text, fontsize=size))
        if width <= 0:
            continue
        sx = float(max(0.2, min(4.0, (x1 - x0) / width)))
        try:
            tw = pymupdf.TextWriter(page.rect)
            tw.append(pymupdf.Point(x0, y1 - (y1 - y0) * 0.18), ln.text,
                      font=font, fontsize=size)
            tw.write_text(page, render_mode=3,
                          morph=(pymupdf.Point(x0, y1), pymupdf.Matrix(sx, 1)))
            written += 1
        except Exception:
            continue  # 个别字符写不进去不该让整页作废
    return written


def scrub_text(page) -> bool:
    """抹掉页面上已有的文字层（保留扫描底图与矢量图）。

    用于 text-garbled 的页面：字体缺 ToUnicode 映射，抽出来是乱码；
    不抹掉的话 OCR 出来的正确文字会和乱码混在一起，检索结果里全是噪声。
    """
    pymupdf = C.import_pymupdf()
    try:
        page.add_redact_annot(page.rect)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        return True
    except Exception as exc:
        C.warn(f"清除旧文字层失败（该页保留原样）：{type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------------------
# 多进程 worker（Windows 用 spawn，函数必须是模块级）
# --------------------------------------------------------------------------
_W: dict = {}


def _worker_init(path: str, dpi: int, engine: str, min_conf: float,
                 password: str | None, opts: dict):
    _W["doc"] = C.open_doc(path, password=password)
    _W["engine"] = build_engine(engine, **opts)
    _W["dpi"] = dpi
    _W["min_conf"] = min_conf


def _worker_page(page_no: int):
    page = _W["doc"][page_no - 1]
    try:
        arr = render_array(page, _W["dpi"])
        lines = _W["engine"].recognize(arr)
    except Exception as exc:
        return page_no, [], f"{type(exc).__name__}: {exc}"
    keep = [ln for ln in lines if ln.conf >= _W["min_conf"] and ln.text.strip()]
    return page_no, [asdict(ln) for ln in keep], ""


def _needs_ocr(text: str, mode: str) -> tuple[bool, str]:
    """'"这一页要不要 OCR"的判断，返回 (要不要, 原因)。"""
    verdict = C.page_text_verdict(text)
    if mode == "force":
        return True, "强制重做"
    if verdict == "":
        return True, "无文字层"
    if verdict == "garbled":
        return True, "文字层乱码"
    return False, "已有文字层"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def ocr_document(path: str, out_pdf: str | None = None,
                 pages: list[int] | None = None,
                 dpi: int = DEFAULT_DPI,
                 engine: str = "auto",
                 jobs: int = 4,
                 min_conf: float = DEFAULT_MIN_CONF,
                 existing: str = "auto",
                 dry_run: bool = False,
                 password: str | None = None,
                 sidecar: str | None = None,
                 quiet: bool = False,
                 **eng_opts) -> dict:
    """给 PDF 加 OCR 文字层。

    返回统计信息（含每页的文字来源与置信度），并按需写出可搜索 PDF 与
    [[p.N]] 页锚全文（sidecar）。
    """
    src = C.ensure_pdf(path)
    doc = C.open_doc(src, password=password)
    page_count = doc.page_count
    targets = [p for p in (pages or range(1, page_count + 1))
               if 1 <= p <= page_count]
    if not targets:
        C.die("页范围为空（扫描版页码换算用 --offset）")

    base_texts = [""] + [doc[i].get_text() for i in range(page_count)]
    todo: list[int] = []
    reasons: dict[int, str] = {}
    stats: dict = {
        "file": src, "pages_total": page_count, "pages_in_scope": len(targets),
        "engine": engine, "dpi": dpi, "min_conf": min_conf, "existing": existing,
        "results": {}, "errors": [], "elapsed": 0.0,
    }
    for p in targets:
        need, why = _needs_ocr(base_texts[p], existing)
        reasons[p] = why
        if need:
            todo.append(p)
        stats["results"][p] = {"orig": C.page_text_verdict(base_texts[p]) or "none",
                               "source": "ocr" if need else "text",
                               "reason": why, "lines": 0, "avg_conf": None}
    stats["pages_to_ocr"] = len(todo)
    stats["pages_skipped"] = len(targets) - len(todo)

    if dry_run:
        stats["plan"] = [{"page": p, "reason": reasons[p]} for p in targets]
        doc.close()
        return stats

    page_lines: dict[int, list[dict]] = {}
    if todo:
        if not quiet:
            print(f"OCR：{len(todo)} 页要做，{stats['pages_skipped']} 页已有文字层跳过"
                  f"（引擎 {engine}，{dpi} dpi，并发 {jobs}）")
        t0 = time.time()
        page_lines, errors = _run_ocr(todo, src, dpi, engine, jobs, min_conf,
                                      password, eng_opts, quiet=quiet)
        stats["elapsed"] = round(time.time() - t0, 1)
        stats["errors"] = errors
    elif not quiet:
        print("范围内每一页都已有可用文字层，不需要 OCR"
              "（确实要重做请加 --existing force）")

    # 写入文字层
    written_pages = total_lines = 0
    confs: list[float] = []
    for p in todo:
        raw = page_lines.get(p, [])
        lines = [OcrLine(**d) for d in raw]
        if not lines:
            stats["results"][p]["source"] = "none"
            continue
        if stats["results"][p]["orig"] == "garbled" or existing == "force":
            scrub_text(doc[p - 1])
        n = insert_text_layer(doc[p - 1], lines, dpi=dpi, min_conf=min_conf)
        written_pages += 1
        total_lines += n
        confs.extend(ln.conf for ln in lines)
        stats["results"][p]["lines"] = n
        stats["results"][p]["avg_conf"] = round(
            sum(ln.conf for ln in lines) / len(lines), 3)

    stats["pages_written"] = written_pages
    stats["lines_written"] = total_lines
    stats["avg_conf"] = round(sum(confs) / len(confs), 3) if confs else None
    for p, msg in stats["errors"]:
        C.warn(f"第 {p} 页 OCR 失败：{msg}")

    # 写出可搜索 PDF
    if out_pdf:
        out = Path(C.norm_path(out_pdf))
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.resolve() == Path(src).resolve():
            C.die("输出路径不能和原文件相同，请用 --out 指定新文件名")
        before = os.path.getsize(src)
        _save_doc(doc, out)
        stats["out_pdf"] = str(out)
        stats["size_before_mb"] = round(before / 1048576, 1)
        stats["size_after_mb"] = round(os.path.getsize(out) / 1048576, 1)
        stats["verify"] = verify_output(str(out), sorted(page_lines)[:3],
                                        password=password)

    # sidecar：整册 [[p.N]] 全文（OCR 页用 OCR 结果，其余页用原文字层）
    if sidecar:
        pairs = []
        for i in range(1, page_count + 1):
            if page_lines.get(i):
                text = "\n".join(d["text"] for d in page_lines[i])
            else:
                text = doc[i - 1].get_text()
            pairs.append((i, text))
        stats["sidecar"] = str(C.write_pages_txt(sidecar, pairs))

    doc.close()
    return stats


def verify_output(path: str, sample_pages: list[int],
                  password: str | None = None) -> dict:
    """回读产物做体检：确认文字层真的写进去了、能搜到。"""
    try:
        doc = C.open_doc(path, password=password)
    except SystemExit:
        return {"ok": False, "note": "产物打不开"}
    out = {"ok": True, "samples": []}
    for p in sample_pages:
        if not (1 <= p <= doc.page_count):
            continue
        page = doc[p - 1]
        text = page.get_text().strip()
        out["samples"].append({"page": p, "chars": len(text),
                               "head": text[:40].replace("\n", "")})
    doc.close()
    return out


def _save_doc(doc, out: Path):
    last_err = None
    for kwargs in ({"garbage": 3, "deflate": True}, {"garbage": 3}, {"deflate": True}, {}):
        try:
            doc.save(str(out), **kwargs)
            return
        except Exception as exc:
            last_err = exc
    C.die(f"保存失败: {last_err}")


def _run_ocr(todo: list[int], src: str, dpi: int, engine: str, jobs: int,
             min_conf: float, password: str | None, eng_opts: dict,
             quiet: bool = False) -> tuple[dict, list]:
    """并发跑 OCR，返回 ({页号: [行 dict]}, [(页号, 错误)])。失败退回单进程。"""
    total = len(todo)
    every = max(1, total // 20)

    def _collect(iterator) -> tuple[dict, list]:
        page_lines: dict[int, list] = {}
        errors: list = []
        done = 0
        for page_no, lines, err in iterator:
            done += 1
            if err:
                errors.append((page_no, err))
            else:
                page_lines[page_no] = lines
            if not quiet:
                C.print_progress(done, total, f"p.{page_no}  行 {len(lines)}", every=every)
        return page_lines, errors

    if jobs > 1 and total > 2:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(
                max_workers=max(1, jobs), initializer=_worker_init,
                initargs=(src, dpi, engine, min_conf, password, eng_opts),
            ) as ex:
                return _collect(ex.map(_worker_page, todo, chunksize=1))
        except Exception as exc:
            C.warn(f"并发 OCR 不可用（{type(exc).__name__}: {exc}），改为单进程。")
    _worker_init(src, dpi, engine, min_conf, password, eng_opts)
    return _collect(_worker_page(p) for p in todo)


def print_report(stats: dict):
    print()
    print(f"范围      : {stats['pages_in_scope']} 页（全书 {stats['pages_total']} 页）")
    print(f"本次 OCR  : {stats['pages_to_ocr']} 页；跳过 {stats['pages_skipped']} 页")
    print(f"写入文字层: {stats.get('pages_written', 0)} 页 / "
          f"{stats.get('lines_written', 0)} 行；平均置信度 {stats.get('avg_conf')}")
    print(f"耗时      : {stats.get('elapsed', 0)} 秒")
    if stats.get("out_pdf"):
        print(f"可搜索 PDF: {stats['out_pdf']}"
              f"（{stats['size_before_mb']} MB → {stats['size_after_mb']} MB）")
    v = stats.get("verify") or {}
    for s in v.get("samples", []):
        print(f"  体检 p.{s['page']}: 可提取 {s['chars']} 字｜{s['head']}")
    if stats.get("sidecar"):
        print(f"页锚全文  : {stats['sidecar']}")
    if stats.get("errors"):
        print(f"失败页面  : {[p for p, _ in stats['errors']][:20]}"
              "（这些页没有文字层，仍可走视觉阅读）")


if __name__ == "__main__":  # 自测入口：python ocrtool.py <pdf> --out x.pdf
    import argparse

    ap = argparse.ArgumentParser(prog="ocrtool.py", description="OCR 文字层（自测入口）")
    ap.add_argument("pdf")
    ap.add_argument("--out")
    ap.add_argument("--pages")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    ap.add_argument("--engine", default="auto")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--min-conf", type=float, default=DEFAULT_MIN_CONF)
    ap.add_argument("--existing", default="auto", choices=["auto", "force", "skip"])
    ap.add_argument("--sidecar")
    a = ap.parse_args()
    d = C.open_doc(a.pdf)
    pg = C.parse_pages(a.pages, d.page_count) if a.pages else None
    d.close()
    st = ocr_document(a.pdf, out_pdf=a.out, pages=pg, dpi=a.dpi, engine=a.engine,
                      jobs=a.jobs, min_conf=a.min_conf, existing=a.existing,
                      sidecar=a.sidecar)
    print_report(st)
