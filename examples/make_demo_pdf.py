#!/usr/bin/env python3
"""造两个演示用的 PDF，方便离线试跑 pdf-toolkit。

生成两份文件：

  demo_text.pdf — 有真实文字层的两页"小书"
      含标题、正文段落、一张带框表格、一张插图、三个表单字段，
      可以拿来试 info / text / search / tables / images / forms / to-ai。

  demo_scan.pdf — 上面那本"扫描件"
      做法是把 demo_text.pdf 的每页渲染成图片，再拼成一个**没有文字层**的新 PDF，
      跟真实扫描书的处境一样：info 会判成 scanned，text 抽出来是空的。
      加 --ocr 之后再抽，就能看到文字层被补回来的效果。

用法：
    <py> examples/make_demo_pdf.py --out .demo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import pymupdf
except Exception:
    sys.exit("需要 PyMuPDF：\n"
             "  python -m venv .venv\n"
             "  .venv/Scripts/python.exe -m pip install -r requirements.txt")

CJK = "china-s"  # PyMuPDF 内置的中日韩字体（Droid Sans Fallback）

PAGE1_PARAS = [
    "我国古代建筑文化遗产极为丰富，以木结构为主体的建筑体系自形成以来，"
    "经历了漫长的历史阶段。在几千年的历史发展中，中国古建筑经过了不断形成、"
    "发展、成熟、演变的过程。各个不同历史时期的建筑在平面布局、立面形式、"
    "构造方式、建筑风格诸方面都形成了不同的风格特点。",
    "明、清两代作为中国古代建筑发展的最后一个阶段，在建筑的形式、构造方式、"
    "建筑材料、工艺技术以及法式则例的遵循方面形成了较为统一的风格，"
    "有很多共同或相似之处。清雍正年间颁行的工部《工程做法则例》就是这个时期的"
    "建筑在造型、设计、构造、用材、工艺及施工技术等方面的总结。",
    "本书研究的问题：明、清两代木构建筑在构件权衡、构造做法与营造技术上的规律，"
    "以及这些规律在今天的文物建筑修缮中如何参考使用。研究这些问题，"
    "需要把官式做法与地方做法分开考察，也需要把文献记载与现存实物对照起来看，"
    "因为二者之间的差异往往正是技术演变留下的痕迹。",
]

TABLE_ROWS = [
    ["构件名称", "尺寸（斗口）", "主要用途"],
    ["柱", "3.0", "主要承重构件"],
    ["额枋", "2.4", "连接柱头，增强整体性"],
    ["斗拱", "1.8", "出挑承檐，传递荷载"],
]

PAGE2_PARAS = [
    "第二节  清代建筑的通则",
    "面宽与进深是确定单体建筑规模的两个基本量。面宽指建筑正面相邻两柱之间的"
    "距离，进深指建筑侧面相邻两柱之间的距离，二者都以斗口为模数单位，"
    "因此斗口尺寸一经确定，整座建筑的尺度序列也就随之确定。",
    "柱高与柱径之间存在固定的比例关系，这一比例在《工程做法则例》中有明确规定，"
    "是清代官式建筑设计中最重要的权衡之一。撑间、举架、收山、推山等做法，"
    "同样是在这套权衡体系内展开的，理解了权衡，才谈得上理解明、清木构的构造。",
]


def add_paragraph(page, y: float, text: str, size: float = 11.5,
                  font: str = CJK, leading: float = 19.0,
                  left: float = 72.0, width: float = 452.0) -> float:
    """按宽度自动折行写一段中文，返回下一段的起始 y。"""
    rect = pymupdf.Rect(left, y, left + width, y + 400)
    # 中文没有空格，PyMuPDF 的 textbox 会按字符宽度折行
    leftover = page.insert_textbox(rect, text, fontsize=size, fontname=font,
                                   lineheight=leading / size, align=0)
    used = 400 - max(0.0, leftover)
    return y + used + 8


def build_text_pdf(path: Path) -> None:
    doc = pymupdf.open()

    # ---- 第 1 页：标题 + 正文 + 表格 + 插图 ----
    page = doc.new_page()
    page.insert_text((72, 90), "第一章  明、清古建筑的形式、种类、通则及权衡",
                     fontsize=17, fontname=CJK)
    y = 125.0
    for para in PAGE1_PARAS:
        y = add_paragraph(page, y, para)

    # 带框表格（find_tables 靠线条识别，所以这里画线）
    y += 6
    cols = [150.0, 110.0, 192.0]
    row_h = 24.0
    x0 = 72.0
    for r, row in enumerate(TABLE_ROWS):
        x = x0
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x, y + r * row_h, x + cols[c], y + (r + 1) * row_h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.7)
            page.insert_text((rect.x0 + 6, rect.y0 + 16), cell,
                             fontsize=10.5, fontname=CJK)
            x += cols[c]
    table_bottom = y + len(TABLE_ROWS) * row_h
    page.insert_text((72, y - 8), "表 1-2  主要木构件权衡表",
                     fontsize=10.5, fontname=CJK)

    # 一张插图：用确定性图案生成有足够信息量的位图，
    # 这样 images 子命令在默认阈值（--min-kb 8）下也能把它导出来。
    # 纯色方块会压到 1 KB 以下，反而不便演示。
    fig_w, fig_h = 320, 200
    samples = bytearray(fig_w * fig_h * 3)
    for yy in range(fig_h):
        for xx in range(fig_w):
            i = (yy * fig_w + xx) * 3
            band = ((xx * 7 + yy * 3) % 23) * 6          # 斜向条纹
            samples[i] = (90 + band + (yy * 255 // fig_h)) % 256
            samples[i + 1] = (110 + band + (xx * 200 // fig_w)) % 256
            samples[i + 2] = (140 + band // 2) % 256
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, fig_w, fig_h))
    pix.samples_mv[:] = bytes(samples)   # 直接写像素缓冲（samples_mv 是可写的）
    fig_top = table_bottom + 34
    page.insert_image(pymupdf.Rect(72, fig_top, 392, fig_top + 200), pixmap=pix)
    page.insert_text((72, fig_top + 220), "图 1-6  木构架示意（演示用占位图）",
                     fontsize=10.5, fontname=CJK)

    # ---- 第 2 页：小标题 + 正文 + 表单字段 ----
    page2 = doc.new_page()
    y = 90.0
    for para in PAGE2_PARAS:
        y = add_paragraph(page2, y, para)

    y += 24
    page2.insert_text((72, y), "（以下为演示表单字段，可用 forms 子命令填写）",
                      fontsize=10.5, fontname=CJK)
    y += 22
    fields = [
        ("reader_name", pymupdf.PDF_WIDGET_TYPE_TEXT, (72, y, 300, y + 20), None),
        ("affiliation", pymupdf.PDF_WIDGET_TYPE_TEXT, (72, y + 30, 300, y + 50), None),
        ("agree_terms", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (72, y + 66, 90, y + 84), None),
        ("level", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, (72, y + 96, 300, y + 116),
         ["入门", "进阶", "专家"]),
    ]
    page2.insert_text((100, y + 15), "姓名", fontsize=10.5, fontname=CJK)
    page2.insert_text((100, y + 45), "单位", fontsize=10.5, fontname=CJK)
    page2.insert_text((100, y + 81), "同意条款", fontsize=10.5, fontname=CJK)
    page2.insert_text((100, y + 111), "阅读深度", fontsize=10.5, fontname=CJK)
    for name, ftype, rect, choices in fields:
        w = pymupdf.Widget()
        w.field_name = name
        w.field_type = ftype
        w.rect = pymupdf.Rect(*rect)
        if choices:
            w.choice_values = choices
            w.field_value = choices[0]
        page2.add_widget(w)

    doc.save(str(path), garbage=3, deflate=True)
    doc.close()


def build_scan_pdf(src: Path, dst: Path, dpi: int = 150) -> None:
    """把有文字层的 PDF 逐页渲染成图片，再拼成没有文字层的"扫描件"。"""
    src_doc = pymupdf.open(str(src))
    out = pymupdf.open()
    for page in src_doc:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, pixmap=pix)
    out.save(str(dst), garbage=3, deflate=True)
    out.close()
    src_doc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 pdf-toolkit 演示 PDF")
    ap.add_argument("--out", default=".demo", help="输出目录（默认 .demo）")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_pdf = out_dir / "demo_text.pdf"
    scan_pdf = out_dir / "demo_scan.pdf"

    build_text_pdf(text_pdf)
    build_scan_pdf(text_pdf, scan_pdf)

    print(f"已生成 {text_pdf}（{text_pdf.stat().st_size / 1024:.0f} KB，有文字层）")
    print(f"已生成 {scan_pdf}（{scan_pdf.stat().st_size / 1024:.0f} KB，无文字层=模拟扫描件）")
    print()
    print("下一步可以试：")
    print(f"  <py> scripts/pdftool.py info {text_pdf}")
    print(f"  <py> scripts/pdftool.py tables {text_pdf} --out {out_dir}/tables --print")
    print(f"  <py> scripts/pdftool.py images {text_pdf} --out {out_dir}/images --list")
    print(f"  <py> scripts/pdftool.py forms {text_pdf} --template {out_dir}/form.json")
    print(f"  <py> scripts/pdftool.py to-ai {text_pdf} --out {out_dir}/ai")
    print(f"  <py> scripts/pdftool.py ocr {scan_pdf} --out {out_dir}/demo_scan_ocr.pdf "
          f"--sidecar {out_dir}/demo_scan.pages.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
