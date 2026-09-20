# 可运行的例子

`make_demo_pdf.py` 会造两份 PDF，让你不用翻自己的文件就能把主要功能跑一遍。
两份都是**合成数据**（不是任何真书的摘录），可以随便改。

```bash
PY=.venv/Scripts/python.exe      # 或 .venv/bin/python

$PY examples/make_demo_pdf.py --out .demo
```

生成两份文件：

| 文件 | 是什么 | 拿来试什么 |
|---|---|---|
| `.demo/demo_text.pdf` | 两页带**真实文字层**的"小书"：标题、中文段落、一张带框表格、一张插图、4 个表单字段 | `info` / `text` / `search` / `tables` / `images` / `forms` / `to-ai` |
| `.demo/demo_scan.pdf` | 上面那本的**模拟扫描件**：把每页渲染成图片再拼回 PDF，**没有文字层** | `info`（判成 `scanned`）→ `ocr` → `search`（活过来） |

## 一、文字层完好的一本（走文本路线）

```bash
$PY scripts/pdftool.py info .demo/demo_text.pdf
#   文字层: text-rich（提取器 pymupdf）；表单字段 4 个；内嵌图片 1 个

$PY scripts/pdftool.py text .demo/demo_text.pdf --pages 1
#   带 [[p.1]] 页锚的正文

$PY scripts/pdftool.py search .demo/demo_text.pdf --pattern 斗口
#   命中页号 + 上下文（> 标出命中行）

$PY scripts/pdftool.py tables .demo/demo_text.pdf --print
#   识别出那张"主要木构件权衡表"，打印成 Markdown 表格

$PY scripts/pdftool.py images .demo/demo_text.pdf --out .demo/images
#   导出图 1-6 那张位图（默认阈值就能过）

$PY scripts/pdftool.py forms .demo/demo_text.pdf
#   列出 4 个字段：reader_name / affiliation / agree_terms / level
```

## 二、模拟扫描件（走 OCR）

```bash
$PY scripts/pdftool.py info .demo/demo_scan.pdf
#   文字层: scanned —— 有图没字

$PY scripts/pdftool.py text .demo/demo_scan.pdf --pages 1
#   抽不出东西（这正是"扫描版"的处境）

$PY scripts/pdftool.py ocr .demo/demo_scan.pdf \
    --out .demo/demo_scan_ocr.pdf --sidecar .demo/demo_scan.pages.txt
#   2 页约十几秒；平均置信度约 0.99

$PY scripts/pdftool.py search .demo/demo_scan_ocr.pdf --pattern 斗拱
#   现在能搜了，返回页号 + 上下文
```

`.demo/demo_scan_ocr.pdf` 用 PDF 阅读器打开，视觉上还是那张扫描图，
但可以选中、可以 Ctrl+F。`.demo/demo_scan.pages.txt` 是带 `[[p.N]]` 页锚的全文。

## 三、打成 AI 友好包

```bash
$PY scripts/pdftool.py to-ai .demo/demo_text.pdf --out .demo/ai --images --tables
```

产物：

```
.demo/ai/
├─ demo_text.md            Markdown 正文（页锚 [[p.N]] + 标题 + 段落 + 表格 + 插图）
├─ demo_text.pages.txt     逐页全文（可直接当 book-kb-builder 的 verify.py --source）
├─ manifest.json           每页的字数/标题/表格/图片/文字来源
├─ tables/p0001_t1.csv
└─ images/p0001_1.png
```

## 四、填表单

```bash
$PY scripts/pdftool.py forms .demo/demo_text.pdf --template .demo/form.json
#   编辑 form.json 填上值，然后：
$PY scripts/pdftool.py forms .demo/demo_text.pdf --fill .demo/form.json --out .demo/filled.pdf
$PY scripts/pdftool.py forms .demo/filled.pdf            # 回读确认
$PY scripts/pdftool.py forms .demo/demo_text.pdf --fill .demo/form.json \
    --flatten --out .demo/flat.pdf                      # 扁平化，字段不再可编辑
```

## 五、拆开再合起来

```bash
$PY scripts/pdftool.py split .demo/demo_text.pdf --every 1 --out .demo/parts
$PY scripts/pdftool.py merge --out .demo/rejoined.pdf --toc-title \
    .demo/parts/demo_text_p001-001.pdf .demo/parts/demo_text_p002-002.pdf
$PY scripts/pdftool.py outline .demo/rejoined.pdf --md
```

`split` 会额外写出 `_split_manifest.json`，记录每个分册对应原书哪些页；
`merge --toc-title` 按来源文件建顶层书签。
