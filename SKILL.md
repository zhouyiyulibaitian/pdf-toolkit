---
name: pdf-toolkit
description: PDF 实操工具箱（命令行）——给扫描版 PDF 加 OCR 可搜索文字层、把 PDF 电子书转成模型好读的 Markdown/页锚全文、抽正文与表格、导出插图、拆分合并、页面增删旋转、读写书签、填 PDF 表单、处理加密与损坏文件。当用户上传或提到 PDF 并想做这些事时使用：扫描版/影印本 OCR、做成可搜索的 PDF、PDF 转 Markdown/文本、提取表格或图片、按章拆分或合并、填表、删页转页、提取目录；也用于配合图书知识库（book-kb-builder）做入库前的文字层准备。Use when the user uploads PDFs or asks to OCR a scanned book, make a searchable PDF, convert a PDF into AI-readable text/Markdown, extract tables or images, split/merge PDFs, fill forms, or prepare a PDF before it goes into a book knowledge base.
---

# pdf-toolkit：把 PDF 处理成能用、能读、能核对的样子

PDF 有一件事最容易被搞错：**扫描版 PDF 里没有文字**（一页就是一张图），
不管怎么"提取"都抽不出东西。所以本 skill 的第一件事永远是**判断文字层**，
再决定走哪条路：

- 有文字层 → 直接抽正文、表格、目录，打 AI 友好包；
- 没有文字层（扫描版/影印/乱码）→ **先加 OCR 文字层**，
  之后它就和正常电子书一样可搜索、可引用、可被机器核对。

加完文字层的 PDF 是本 skill 最有价值的产物：原件视觉上分毫未变，
但能选中、能 Ctrl+F、能被文字提取器读到。

## 产出物在哪

工作目录随命令的 `--out` 走。默认位置：

- `ocr` → 与原件同目录的 `<书名>_ocr.pdf`
- `to-ai` → 与原件同目录的 `<书名>_ai/`（Markdown + 页锚全文 + manifest + 表格/图片）
- `render` → 当前目录 `pdf_pages/`
- `split` → 你给的 `--out` 目录
- `tables` / `images` → 你给的 `--out` 目录

`--sidecar` 写出的 `<书名>.pages.txt` 是**整册带 `[[p.N]]` 页锚的全文**，
格式与 book-kb-builder 的 `booktool.py probe` 产出一致，可直接当 `verify.py --source`。

## 命令怎么跑

脚本在本 skill 目录的 `scripts/` 下，用本 skill 自带 venv 里的 python：

```bash
<本skill目录>/.venv/Scripts/python.exe scripts/pdftool.py <子命令> ...   # Windows
<本skill目录>/.venv/bin/python       scripts/pdftool.py <子命令> ...   # macOS / Linux
```

先跑一次自检确认环境（PyMuPDF 与 OCR 引擎是否就绪）：

```bash
<py> scripts/pdftool.py check
```

需要装依赖时**先告诉用户**再装（会改动环境），优先装进本 skill 的 venv：
`"<venv python>" -m pip install pymupdf rapidocr onnxruntime pillow`

## 工作流程

### 第 1 步：先看这是什么 PDF（别跳过）

```bash
<py> scripts/pdftool.py info "<文件.pdf>"
```

`info` 给出页数、文字层判定、书签质量、表单/图片/注释数量，并直接给出下一步建议。
文字层判定与 book-kb-builder **同阈值**，所以两个 skill 不会互相打脸：

| 判定 | 含义 | 走哪条路 |
|---|---|---|
| `text-rich` | 文字层完好 | 文本路线：`text`/`search` 精读，`to-ai` 打包 |
| `text-partial` | 部分页有文字（图版书、混合型） | 正文走文本路线，图版页出图视觉判读 |
| `text-garbled` | 有字符但乱码（字体缺 ToUnicode 映射） | 先 `ocr`（会自动抹掉乱码层） |
| `scanned` | 没有文字层（纯图像） | 先 `ocr`，或直接 `render` 视觉阅读 |

### 第 2 步：按需求分诊

| 用户想要 | 跑什么 |
|---|---|
| "把这本书变成模型能读的" | `to-ai`（扫描版加 `--ocr`，或先单跑 `ocr`） |
| "做成能搜索的 PDF" / "OCR 一下" | `ocr` |
| "把表格导出来" | `tables --print` 先看，再 `--out DIR` |
| "把图片导出来" | `images --out DIR`（整页图用 `--render-pages`） |
| "拆成几册" / "按章拆" | `split` |
| "合并成一个 PDF" | `merge` |
| "删掉第 3 页" / "这页转正" | `pages` |
| "填这个表单" | `forms`（先 `--template` 拿字段名） |
| "目录/书签呢" | `outline` |
| "看看第 188 页说了什么" | `text --pages 188`，或 `render --pages 188` 出图 |

### 第 3 步：扫描版先加文字层（本 skill 的核心动作）

```bash
<py> scripts/pdftool.py ocr "<书.pdf>" --out "<书_ocr.pdf>" --sidecar "<书.pages.txt>"
```

- 先 `--dry-run` 看会做哪些页、要多久；只做某几章用 `--pages`（记得 `--offset` 换算）。
- 速度：单页约 4-5 秒（300 dpi），`--jobs` 默认按 CPU 核数/3；几百页的书十几分钟。
- 质量：中文扫描书平均置信度约 0.98。**但 OCR 文字是拐杖不是权威转录**——
  数字、年代、专有名词要引用时回原页面图核对。
- 已有文字层的页会被跳过（`--existing auto`）；乱码页会先抹掉旧文字层再写新的。

细节、参数取舍、识别错误规律见 `references/ocr.md`。

### 第 4 步：打包成 AI 友好形式（要读整本书时）

```bash
<py> scripts/pdftool.py to-ai "<书.pdf>" --out "<输出目录>" --images --tables
```

产出 `书.md`（页锚 `[[p.N]]` + 标题 + 段落 + Markdown 表格 + 插图引用）、
`书.pages.txt`（机器核对用）、`manifest.json`（每页的字数、标题、表格、图片、
以及**文字来源** `text`/`ocr`/`none`）、`tables/`、`images/`。

`manifest.json` 的每页 `source` 是关键：
引用原书文字层（`text`）可以逐字照抄；引用 OCR（`ocr`）的内容要标注并回图核对；
`none` 的页就是纯图版，不要假装读过。

格式约定、manifest 字段、已知限制见 `references/ai-package.md`。

### 第 5 步：与图书知识库协作（用户要"入库/放进知识库"时）

分工：**本 skill 负责把 PDF 弄成可读可核对的形态，book-kb-builder 负责读、分类、
写总结、防编造校验、落库。** 不要越界去建知识库或写阅读总结。

最常用的交接（扫描版）：

```bash
<py> scripts/pdftool.py ocr "<书.pdf>" --out "<书_ocr.pdf>" --sidecar "<书.pages.txt>"
# 然后交给 book-kb-builder：书用 <书_ocr.pdf>，校验的 --source 用 <书.pages.txt>
```

加完 OCR 后，booktool 会把这本原本 `scanned` 的书判成 `text-rich`，
于是它就能走文本路线、引文能被机器核对，不必全程靠看页面图。

三条纪律（详细版见 `references/interop-book-kb.md`）：
1. 引文核对失败先怀疑 OCR 认错字，回原页面图核实，别急着删引文；
2. 数字与专有名词必须回图核对；
3. 覆盖度声明里写明"文字层由 OCR 生成"。

### 第 6 步：汇报

告诉用户这几件事：产物路径（绝对路径）、改了/加了什么、哪些页没有文字层、
OCR 的置信度与可疑之处、以及**你自己不确定的地方**。
不要让用户以为 OCR 文本等于原书排版。

## 工具速查

| 目的 | 命令 |
|---|---|
| 环境自检（引擎/依赖） | `pdftool.py check` |
| 看这本书：页数/文字层/书签/表单/图片 | `pdftool.py info <pdf> [--json]` |
| 抽正文（带 `[[p.N]]` 页锚） | `pdftool.py text <pdf> --pages 5-12 [--mode markdown]` |
| 关键词定位（返回页号+上下文） | `pdftool.py search <pdf> --pattern 词 [--pages R]` |
| 打 AI 友好包 | `pdftool.py to-ai <pdf> --out DIR [--images] [--tables] [--ocr]` |
| 加 OCR 可搜索文字层 | `pdftool.py ocr <pdf> --out X_ocr.pdf [--pages R] [--sidecar f.txt]` |
| 抽表格 → CSV | `pdftool.py tables <pdf> [--pages R] --out DIR [--print]` |
| 导图片 / 出整页图 | `pdftool.py images <pdf> --out DIR [--render-pages] [--list]` |
| 渲染页面为 PNG（视觉阅读） | `pdftool.py render <pdf> --pages R [--dpi 300]` |
| 拆分 | `pdftool.py split <pdf> --out DIR [--by-bookmarks\|--ranges\|--every N\|--size MB]` |
| 合并 | `pdftool.py merge --out all.pdf a.pdf b.pdf [--toc-title]` |
| 页面增删/旋转/重排 | `pdftool.py pages <pdf> --out new.pdf [--select R] [--delete R] [--rotate R:90]` |
| 表单查看/填写/扁平化 | `pdftool.py forms <pdf> [--list\|--template t.json\|--fill d.json --out f.pdf]` |
| 书签导出/写入 | `pdftool.py outline <pdf> [--json\|--md\|--chapters]`、`--set toc.json --out new.pdf` |

多文件批量任务（一次处理一堆 PDF）就逐个跑上面这些命令，
都在一个 CLI 里，不要为了批量另写脚本。

## 遇到这些情况怎么办

- **"OCR 一下这本书"但它是 text-rich**：先说明"这本书已经有文字层，不需要 OCR"，
  直接抽文本即可。对 text-rich 的书跑 OCR 是白等十几分钟。
- **扫描版只想要文本、不想要 PDF**：`ocr --no-pdf --sidecar out.pages.txt`。
- **`text` 抽出来是空的或乱码**：正常，说明是扫描版/缺映射字体。
  别反复换提取器，转 OCR 或视觉路线。
- **表格识别不到**：扫描版没有矢量线条，`find_tables` 抓不到，这是正常的。
  无框表格可试 `--table-strategy text`；其余情况只能视觉判读后人工整理。
- **整页都是图、`images` 什么都没导出来**：默认把占页面 85% 以上的图当作
  "整页扫描底图"跳过。要导整页图用 `--render-pages`，或加 `--page-scans-are-figures`。
- **XFA 表单填不了**：字段列得出来但改不了，脚本会明说。请用 Adobe Acrobat 之类工具。
- **加密 PDF**：所有命令都支持 `--password`；口令不对会明确报错，不会去猜。
- **PDF 有损坏**：能打开时会提示已自动修复，并建议换完整文件。
- **EPUB / MOBI / DJVU**：本 skill 只管 PDF。EPUB 交给 book-kb-builder；
  MOBI/AZW3 先用 Calibre 转格式。
- **书有 800 页以上**：先 `--pages` 跑一小段确认质量，再整册 OCR；
  确认磁盘留出原文件两倍空间（OCR 后大约增加 2-4 MB 的字体与文字层）。
- **用户只要"看"不想处理**：`render --pages R --dpi 300` 出图，
  然后用 Read 逐张看，只记录真正看到的内容。

## 为什么这么做

- **机械判断交给脚本，判断留给模型**：文字层质量、页锚范围、表格版式、
  拆分边界、表单字段类型都是脚本能准确回答的问题，模型只该决定"下一步做哪件事"。
- **文字层优先于视觉阅读**：加一次 OCR（几分钟），换来整本书可搜索、可引用、
  可被 `verify.py` 机械核对引文——这比让模型逐页看图既快又不容易编造。
- **不伪造文字**：没有文字层的页只写占位说明，不生成"像书里的话"。
  知识库里最贵的错误是看起来很像原文的编造。
- **页锚用 PDF 物理页号**：无歧义、可直接跳转、能被脚本检查范围；
  印刷页码的换算交给 book-kb-builder 在校准后记录。
- **OCR 文字标注来源**：`manifest.json` 里区分 `text`/`ocr`/`none`，
  因为这三类内容可信度不同，混在一起之后就再也分不开了。
