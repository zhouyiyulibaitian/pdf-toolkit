# pdf-toolkit

> 一个 PDF 命令行工具箱：给扫描版 PDF 加 **OCR 可搜索文字层**、把 PDF 电子书转成
> **带页锚的 AI 可读文本**、抽正文与表格、导出插图、拆分合并、页面增删旋转、读写书签、填表单。
>
> A command-line PDF toolkit as a ZCode skill: add searchable OCR text layers to scanned PDFs,
> turn books into page-anchored AI-readable Markdown, extract text/tables/images, split & merge,
> edit pages and bookmarks, and fill PDF forms.

为**扫描版图书**（老书、影印本、图册）和**需要把 PDF 喂给模型**的场景写的。

---

## 它解决什么问题

1. **扫描版 PDF 里没有文字。** 一页就是一张图，`pdftotext`、`get_text()` 全都抽不出来。
   常规做法是让模型逐页看图——慢、贵，而且**没法机械核对引用**。
2. **PDF 里的内容不可回查。** 从 PDF 里摘出来的东西，过两周没人说得清出自哪一页。
3. **零碎操作每次都要现写脚本。** 抽表格、抠图、按章拆分、填表单，都是十分钟的事，
   但每次都要重新翻 PyMuPDF 文档。

本工具把这三件事变成一条命令，并且**产出物自带出处**。

## 四个核心能力

### 1. 给扫描版加 OCR 可搜索文字层（`ocr`）

图像原样保留 + 一层**不可见文字层**（与 ocrmypdf 同一思路）：视觉上还是那张扫描图，
但能选中、能 Ctrl+F、能被文字提取器读到。

- **默认零系统依赖**：OCR 走 pip 装的 RapidOCR（ONNX 推理，PP-OCRv6 中文模型随包提供，
  完全离线）。很多机器没权限装 Tesseract——这条路线不需要管理员权限。
  装了 tesseract / ocrmypdf 会被自动优先使用。
- 文字按 OCR 框**逐行摆放并水平缩放**，所以搜索命中的高亮矩形落在真字上，不是缩成一团。
- 已有文字层的页会跳过（`--existing auto`）；`text-garbled`（字体缺 ToUnicode 映射的乱码层）
  会先抹掉旧文字层再写新的——不抹的话正确文字会和乱码混在一起，检索结果全是噪声。
- 副产品 `--sidecar` 输出**整册 `[[p.N]]` 页锚全文**，可直接当 `verify.py --source`。

实测（12 核 CPU，396 页大开本扫描件，300 dpi）：**约 4–5 秒/页，平均置信度 0.98**，
`--jobs 4` 时整册约 10 分钟；文件只增大 2–4 MB（嵌入的中文字体）。

### 2. 打成 AI 友好包（`to-ai`）

```bash
<pdftool> to-ai "书.pdf" --out 输出目录 --images --tables
```

```
输出目录/
├─ 书名.md              Markdown 正文（页锚 + 标题 + 段落 + 表格 + 插图位置）
├─ 书名.pages.txt       带 [[p.N]] 页锚的逐页全文
├─ manifest.json        机器可读索引：每页的字数/标题/表格/图片/文字来源
├─ tables/              每张表一个 CSV（UTF-8 BOM，Excel 直接打开不乱码）
└─ images/              每张插图一个文件
```

两个关键设计：

- **页锚 `[[p.N]]` 用 PDF 物理页号**（不是印刷页码）：无歧义、可直接跳转、能被脚本检查范围。
  写总结时转成 `(p.N)`，人翻到第 N 页就能核对。
- **每页都标注文字来源**（`manifest.json` 的 `pages[].source`）：
  `text` = 原 PDF 自带文字层（可信，可逐字引用）、`ocr` = 本工具 OCR 出来的
  （措辞可信，数字与专有名词要回图核对）、`none` = 纯图版页（没有文本，别假装读过）。

没有文字层的页面**只写占位说明，不生成"像书里的话"**。知识库里最贵的错误是
看起来很像原文的编造。

### 3. 抽正文、表格、图片

| 命令 | 作用 |
|---|---|
| `text` | 按页抽正文，带 `[[p.N]]` 页锚；`--mode markdown` 附标题/表格/段落回拼 |
| `search` | 关键词定位，返回页号 + 上下文（扫描版会直接拒绝并提示先 OCR） |
| `tables` | 表格 → CSV / Markdown / JSON（`--print` 先看效果再全书跑） |
| `images` | 导出内嵌位图（可去重、可跳过整页扫描底图，并报告每类跳过了多少张） |
| `render` | 把指定页渲染成 PNG，供视觉判读或人工核对 |

### 4. 拆分合并、页面手术、表单、书签

- `split`：按**页数 / 页范围 / 书签 / 文件大小**切分，附 `_split_manifest.json`
  记录每个分册对应原书哪些页、标题是什么。书签是扫描流水线生成的占位名
  （`fow001`、`000123`）时会**拒绝**按书签切，并给出替代做法。
- `merge`：按顺序合并，可沿用来源书签（`--toc-title` 按文件建顶层条目）或 `--toc <json>` 手工给目录。
- `pages`：删页 / 选页 / 旋转（累加）/ 倒序。
- `forms`：列出字段与选项、导出填写模板、按 JSON 或 `键=值` 回填、扁平化。
  XFA 表单会明确说"填不了"，不会假装成功。
- `outline`：读书签、导出 JSON/Markdown、写入自定义书签；自动识别并拒绝扫描占位书签。

加密 PDF 全部命令支持 `--password`；结构损坏会提示已自动修复并建议换文件；
遇到 EPUB/MOBI 会明确说"请交给 book-kb-builder / Calibre"，而不是抛一堆栈。

## 与 book-kb-builder 的关系

两个 skill 是上下游，**不抢活**：

| | pdf-toolkit（本仓库） | [book-kb-builder](https://github.com/zhouyiyulibaitian/book-kb-builder) |
|---|---|---|
| 负责 | 把 PDF 变成可读、可查、可核对的形式 | 读、分类、写总结、防编造校验、落库 |
| 产出 | 可搜索 PDF、AI 友好包、页锚全文、表格/图片 | 知识库目录 + 阅读总结 |

交接契约（两边一致）：

- 页锚统一是 `[[p.N]]`（PDF 物理页号）；
- 本工具的 `.pages.txt` 与 `booktool.py probe` 产出**逐字节一致**，可直接当 `verify.py --source`；
- 本工具 OCR 过的页面会带 `ocr-cjk` 字体标记，`to-ai` 靠它关掉"按字号判标题"
  （OCR 的字号是估出来的，会把表格内容误判成章节名）。

**实测的交接效果**：一本 `文字层: scanned` 的扫描书，经 `pdftool.py ocr` 加了文字层之后，
`booktool.py probe` 把它判成 **`text-rich`**，于是这本书就能走文本路线、引文能被机器核对，
不必全程靠看页面图。反向也验过：用 `.pages.txt` 当 `verify.py --source`，
真实引文核对通过、故意编造的引文被抓出来。

## 安装

把本仓库放到 ZCode 的 skills 目录（`~/.agents/skills/pdf-toolkit/`），然后：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt           # macOS / Linux
```

装完自检：

```bash
.venv/Scripts/python.exe scripts/pdftool.py check
# OCR 引擎可用性、pdftotext 是否存在、建议的 --jobs 全在这里
```

| 依赖 | 用途 | 缺了会怎样 |
|---|---|---|
| **PyMuPDF** | 读写 PDF、渲染页面、识别表格、填表单 | **全不可用**（核心依赖） |
| **RapidOCR + onnxruntime + pillow** | 扫描版 OCR | `ocr` 与 `to-ai --ocr` 不可用，其余照常 |
| `tesseract`（可选） | 备选 OCR 引擎 | 不影响；装了自动优先 |
| `ocrmypdf`（可选） | 整册 OCR 流水线（纠偏/分栏/压缩） | 不影响；装了自动优先 |

脚本会把 Git-Bash 风格的 `/c/Users/x` 路径自动转成 Windows 路径，从 shell 直接传路径不用手工转换。

## 快速上手

```bash
T=scripts/pdftool.py
PY=.venv/Scripts/python.exe      # 或 .venv/bin/python

# 0) 先看这是什么 PDF：页数、文字层判定、书签质量、表单/图片数量 + 下一步建议
$PY $T check
$PY $T info "我的书.pdf"

# 1a) 文字层完好 → 直接抽
$PY $T search "我的书.pdf" --pattern 斗拱
$PY $T text   "我的书.pdf" --pages 188-190
$PY $T to-ai  "我的书.pdf" --out 书_ai --images --tables

# 1b) 扫描版 → 先加文字层（几分钟换整本可搜索、可核对）
$PY $T ocr "我的书.pdf" --out "我的书_ocr.pdf" --sidecar "我的书.pages.txt"
$PY $T info "我的书_ocr.pdf"          # 现在应该判成 text-rich / text-partial

# 2) 只要某一章的表格 / 图片
$PY $T tables "我的书.pdf" --pages 100-200 --out tables_dir --print
$PY $T images "我的书.pdf" --out imgs --dedupe

# 3) 拆分 / 合并 / 页面手术
$PY $T split "我的书.pdf" --ranges "1-40,41-88" --out parts
$PY $T merge --out "全卷.pdf" --toc-title vol1.pdf vol2.pdf
$PY $T pages "我的书.pdf" --out clean.pdf --delete 3,17 --rotate 12:90

# 4) 表单
$PY $T forms "form.pdf" --template tpl.json
$PY $T forms "form.pdf" --fill tpl.json --out filled.pdf
```

完整工作流、参数取舍、OCR 质量规律、与知识库的交接细节见
[`SKILL.md`](SKILL.md) 与 [`references/`](references/)（ocr / ai-package / interop-book-kb / recipes）。

## 可运行的例子

`examples/` 里有一个自包含演示：造一份**带文字层的两页小书**（含表格、插图、表单字段）
和它的**模拟扫描件**，然后跑一遍抽取、OCR、打包、合并。

```bash
$PY examples/make_demo_pdf.py --out .demo
$PY $T info .demo/demo_text.pdf                       # text-rich
$PY $T info .demo/demo_scan.pdf                       # scanned（同一个内容的"扫描件"）
$PY $T ocr  .demo/demo_scan.pdf --out .demo/scan_ocr.pdf --sidecar .demo/scan.pages.txt
$PY $T search .demo/scan_ocr.pdf --pattern 斗拱        # 加完文字层就能搜了
```

详见 [`examples/README.md`](examples/README.md)。

## 已知限制（不是 bug，是边界）

- **矢量插图抓不到。** `images` 导的是内嵌位图；线条画的工程图要用 `render` 出整页图看。
- **扫描版识别不出表格。** `tables` 依赖矢量线条/文本对齐；扫描件的表格只能视觉判读后整理。
- **OCR 文字是拐杖，不是权威转录。** 形近字（橡/様、券/卷）和表格里的数字最容易被认错。
  引用措辞、尤其是数字与专有名词时，回原页面图核对一眼
  （`pdftool.py render --pages N --dpi 300`）。
- **XFA 表单填不了。** 字段列得出来但改不了，脚本会明说，请用 Adobe Acrobat 之类工具。
- **只处理 PDF。** EPUB 交给 book-kb-builder，MOBI/AZW3 先用 Calibre 转换。

## 实测数据

| 项目 | 数值 |
|---|---|
| OCR 速度 | 约 4–5 秒/页（300 dpi，单进程）；`--jobs 4` 时 396 页约 10 分钟 |
| OCR 质量 | 中文扫描书平均置信度 0.98–0.99（正文段落几乎可逐字读） |
| OCR 后体积 | 64.8 MB → 66.4 MB（+2–4 MB：嵌入中文字体与文字对象） |
| 文字层判定 | 与 book-kb-builder 同阈值，两个 skill 对同一本书结论一致 |

## 许可

本仓库尚未选择开源许可证（默认保留所有权利）。如需他人可自由使用，
建议补一个 [MIT](https://choosealicense.com/licenses/mit/) 或
[Apache-2.0](https://choosealicense.com/licenses/apache-2.0/)。
