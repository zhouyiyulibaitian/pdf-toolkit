# AI 友好包：`to-ai` 产出什么、怎么用

`to-ai` 把一本 PDF 变成一包"给模型读"的文件。核心不是把 PDF 换个格式，
而是**让每一段内容都能回到原书的具体页**——模型写总结、答问题时可以带 `(p.N)`，
人拿着 PDF 翻到第 N 页就能核对。

```bash
<py> scripts/pdftool.py to-ai "<书.pdf>" --out "<输出目录>" --images --tables
```

## 产物

```
<输出目录>/
├─ <书名>.md              Markdown 正文（页锚 + 标题 + 段落 + 表格 + 插图位置）
├─ <书名>.pages.txt       带 [[p.N]] 页锚的逐页全文（机器核对用）
├─ manifest.json          机器可读索引：每页的标题/表格/图片/文字来源
├─ tables/                每张表一个 CSV（--tables）
└─ images/                每张插图一个文件（--images）
```

## 页锚约定

- `[[p.N]]` —— N 是 **PDF 物理页号**（从 1 开始），不是书上印的页码。
  理由：物理页号无歧义、可以直接在 PDF 阅读器里跳转、也能被脚本检查范围。
- 写总结、写回答时把它转成 `(p.N)`，与 book-kb-builder 的约定完全一致。
- 印刷页码与 PDF 页号的差值（扫描版必做的那次校准）由 book-kb-builder 负责，
  记录在它总结的「内容结构与章节地图」里。本工具不猜这个偏移。

## `<书名>.md` 的结构

```markdown
# 书名

> 来源：xxx.pdf｜396 页｜本次打包 21 页｜文字层 scanned（已用 OCR 补文字层）
> 页锚：`[[p.N]]` 中的 N 是 PDF 物理页号（从 1 开始），回查时直接翻到第 N 页
> 生成：pdf-toolkit to-ai（2026-09-20 17:53）

## 结构地图（来源：PDF 书签 / 正文标题推断）

- 第一章 ……（PDF p.1–p.14，14 页）
  - 第一节 ……（PDF p.1–p.5，5 页）

---

[[p.1]]

# 第一章 ……

正文段落……

| 表头 | 表头 |
| --- | --- |
| 单元格 | 单元格 |

![图注或「PDF p.N 插图」](images/p0040_1.png)
*图 1-2 斗拱构造*
```

要点：

- **页块**以 `[[p.N]]` 开头，便于 grep 定位；正文按阅读顺序排列（标题、段落、
  表格、插图按纵向坐标交错）。
- **标题**来自两处：PDF 书签（可靠）与正文识别。真实文字层的书按字号 +
  「第X章/第X节/Chapter N」等文字特征判断；OCR 出来的页面**只认文字特征**，
  因为那种字号是估出来的。
- **段落**做了回拼：把"一行一个块"的碎片按几何关系接回段落
  （西文行尾连字符合并、中文之间不加空格）。
- **页眉页脚**默认丢掉（贴边 + 短文本，如书名、页码）。要保留加 `--keep-headers`。
- **没有文字层的页**会写一行占位说明，提醒你要么 OCR、要么视觉阅读——
  它**不会**凭空补内容。
- **表格**只在页面有矢量线条/对齐文本时才识别得出。扫描版识别不到表格，
  这是正常的，不是 bug。

## `<书名>.pages.txt`

格式与 book-kb-builder 的 `booktool.py probe` 产出**逐字节一致**：

```
[[p.1]]
第 1 页的正文……

[[p.2]]
第 2 页的正文……
```

所以它可以直接当 `verify.py --source` 用（引文能被机器核对）。
注意：扫描版的空页只有 `[[p.N]]` 标记、没有正文——那是如实记录，不是漏抽。

## `manifest.json`

```json
{
  "source": {"file": "...", "pages": 396, "size_mb": 64.8, "metadata": {...}},
  "text_layer": {"verdict": "scanned", "per_page_chars": 1.0, "pages_with_text": 0},
  "ocr_pdf": "<输出目录>/xxx_ocr.pdf",
  "outline": {"entries": 396, "degenerate": true, "junk_ratio": 1.0},
  "structure_map": {"source": "正文标题推断", "chapters": [{"title": "...", "start": 1, "end": 14}]},
  "outputs": {"markdown": "...", "pages_txt": "...", "tables_dir": "...", "images_dir": "..."},
  "counts": {"tables": 0, "images": 0, "pages_without_text": 0},
  "tables": [{"page": 12, "idx": 1, "file": "tables/p0012_t1.csv", "rows": 4, "cols": 3}],
  "pages": [
    {"page": 1, "chars": 1241, "source": "ocr",
     "headings": ["第一章 ……"], "tables": [], "images": []}
  ]
}
```

**`pages[].source` 是这张表最该看的一列**，三个值：

| 值 | 含义 | 可信度 |
|---|---|---|
| `text` | 原 PDF 就有的文字层 | 高，可逐字引用 |
| `ocr` | 本工具 OCR 出来的 | 中，措辞可信、数字与专有名词需回图核对 |
| `none` | 该页没有文字（纯图版） | 没有文本，只能视觉阅读 |

要"只挑原书文字层"的内容来引用，就按 `source == "text"` 过滤；
要用 OCR 内容，就按 `source == "ocr"` 过滤并在结论里说明。

## 什么时候用哪个入口

| 想要 | 用什么 |
|---|---|
| 整本书给模型读 | `to-ai`（默认） |
| 只要某几页的正文 | `text --pages` |
| 只要表格 | `tables --out DIR --print` |
| 只要图片/图版页 | `images --out DIR`（`--render-pages` 出整页图） |
| 只要给知识库当 `--source` | `to-ai --no-pages-txt` 之外的默认产出，或 `ocr --sidecar` |

## 已知限制（别把这些当成 bug）

- **矢量插图抓不到**：只有位图能被导出；线条画的图（很多工程图）要用
  `render` 出整页图看。
- **整页扫描底图不算插图**：默认跳过（它占页面 85% 以上面积），
  要它当图导加 `--page-scans-are-figures`。
- **表格识别依赖线条**：扫描版、无框表格容易识别不到，可用 `--table-strategy text` 再试。
- **大书很慢**：`--tables` 每页要跑版式分析（几十到几百毫秒），几百页的书建议先
  `--pages` 限定范围，确认有价值再全书跑。
