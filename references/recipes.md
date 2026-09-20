# 常见任务配方

下面都是可以直接照抄的命令。`<py>` 指本 skill 自带 venv 里的 python，
`<T>` 指 `scripts/pdftool.py`。

## 把扫描版电子书变成"能读、能搜、能引用"的形式

```bash
<py> <T> info "book.pdf"                                    # 先确认是 scanned
<py> <T> ocr "book.pdf" --out "book_ocr.pdf" --sidecar "book.pages.txt"
<py> <T> to-ai "book_ocr.pdf" --out "book_ai" --images --tables
```

`book_ocr.pdf` 给人用（能选中、能搜索），`book_ai/` 给模型用，
`book.pages.txt` 给 `verify.py` 用。

## 只 OCR 某一章

```bash
<py> <T> outline "book.pdf" --chapters          # 有书签时直接看章节页范围
<py> <T> render "book.pdf" --pages 20-26 --dpi 150   # 没书签就出目录页的图来看
<py> <T> ocr "book.pdf" --pages 120-180 --out "ch6_ocr.pdf" --offset 39
```

`--offset` 语义：印刷页码 + offset = PDF 页号（本页例子里，书上第 81 页 = PDF 第 120 页）。

## 把整本书的表格导成 CSV

```bash
<py> <T> tables "book.pdf" --out tables_dir --print        # 先小范围看看对不对
<py> <T> tables "book.pdf" --pages 100-200 --out tables_dir --json
```

先 `--print` 看几张，确认表头和列对得上，再全书跑。
CSV 是 UTF-8 with BOM，Excel 直接双击打开不乱码。
扫描版没有矢量线条，识别不到表格是正常的——那种表只能视觉判读后人工整理。

## 导出插图 / 图版页

```bash
<py> <T> images "book.pdf" --out imgs --list               # 先看有多少张、多大
<py> <T> images "book.pdf" --out imgs --dedupe --min-kb 20 # 导出去重的图
<py> <T> images "book.pdf" --out pages --render-pages --pages 30-40 --dpi 200
```

## 按章拆分成多个 PDF

```bash
<py> <T> split "book.pdf" --by-bookmarks --out parts            # 书签可靠时
<py> <T> split "book.pdf" --ranges "1-40,41-88,89-140" --out parts
<py> <T> split "book.pdf" --every 50 --out parts --limit 20     # 先试切前 20 个
```

拆完会生成 `_split_manifest.json`，记录每个文件对应原书哪些页、标题是什么。
书签是扫描流水线生成的那种（fow001/000123）时脚本会拒绝按书签拆，
这时用印刷目录的页码（经 offset 换算成 PDF 页号）喂 `--ranges`。

## 合并

```bash
# 分册合并，并按来源文件建目录
<py> <T> merge --out "全卷.pdf" --toc-title vol1.pdf vol2.pdf vol3.pdf

# 手工给目录（[[级别, 标题, 页码], ...]）
<py> <T> merge --out "全卷.pdf" a.pdf b.pdf --toc toc.json

# 沿用来源自己的书签（默认行为，层级 ≤3）
<py> <T> merge --out "合订.pdf" a.pdf b.pdf
```

合并是按命令行顺序拼的，页码从新文件的第 1 页重新算——脚本会打印每个来源的页范围对应关系。

## 给 PDF 加/改目录

```bash
<py> <T> outline "book.pdf" --json > toc.json        # 导出（可编辑）
<py> <T> outline "book.pdf" --md                    # 给人看的嵌套列表
<py> <T> outline "book.pdf" --set toc.json --out "book_toc.pdf"   # 写回新文件（不动原件）
```

`toc.json` 格式：`[[1, "第一章 …", 40], [2, "第一节 …", 42], ...]`（页码是 PDF 页号）。

## 页面手术：删错页、换顺序、转正

```bash
<py> <T> pages "book.pdf" --out "clean.pdf" --delete 3,17      # 删掉扫描进来的空白/重复页
<py> <T> pages "book.pdf" --out "p1-50.pdf" --select 1-50      # 只留前 50 页
<py> <T> pages "book.pdf" --out "fix.pdf" --rotate 12:90       # 第 12 页转 90°
<py> <T> pages "book.pdf" --out "fix.pdf" --rotate-all 180     # 整册倒过来
```

旋转是**累加**到已有旋转上的，转两次 180° 会回到原样。

## 填 PDF 表单

```bash
<py> <T> forms "form.pdf"                              # 列出字段名、类型、当前值、选项
<py> <T> forms "form.pdf" --template tpl.json          # 导出填写模板
# 编辑 tpl.json 填上值，然后：
<py> <T> forms "form.pdf" --fill tpl.json --out "filled.pdf"
<py> <T> forms "form.pdf" --fill tpl.json --flatten --out "flat.pdf"   # 扁平化，不可再改
<py> <T> forms "form.pdf" --set 姓名=张三 --set 部门=设计部 --out "out.pdf"
```

字段名必须与 `--list` 里的一字不差（大小写敏感）；给错了脚本会提示可用字段名。
复选框给 `true/false` 或 `是/否` 都行；下拉框的值必须来自它的选项列表。

**XFA 表单**（老式政府/银行表格常见）：字段能列出来但改不了，
脚本会明确告诉你，请改用 Adobe Acrobat 之类支持 XFA 的工具。

## 核对一句话是不是原书里的（配合知识库）

```bash
<py> <T> search "book.pdf" --pattern 斗拱 --max-hits 5      # 定位：返回页号 + 上下文
<py> <T> text "book.pdf" --pages 188                        # 精读命中页
<py> <T> render "book.pdf" --pages 188 --dpi 300             # 出图，肉眼核对
```

`search` 每页只报第一处命中（避免同页刷屏），命中行前面用 `>` 标出。
扫描版会直接拒绝检索并提示先 OCR——这是有意的，免得你对着空结果反复换词。

## 排查"抽出来的字是乱的"

```bash
<py> <T> info "book.pdf"                                  # 看文字层判定
<py> <T> text "book.pdf" --pages 10 --method pdftotext    # 换一个提取器对比
```

两个提取器都出乱码 → 字体缺 ToUnicode 映射，走 OCR。
`pymupdf` 与 `pdftotext` 结论不一致时，`info` 会打印两者的质量分对比。

## 大文件与异常文件

- **几百页的扫描书**：先 `--pages` 做一小段试质量，再整册跑；跑之前确认磁盘有
  原文件两倍的空间（OCR 后的文件会大 2-4 MB 左右）。
- **加密 PDF**：所有命令都支持 `--password`。口令不对会明确报错，不会去猜。
- **结构损坏**：能打开时会提示"已由 MuPDF 自动修复"，并建议换一份完整文件。
- **EPUB/MOBI**：本工具只管 PDF。EPUB 交给 book-kb-builder（它支持 epub）；
  MOBI/AZW3 先用 Calibre 转换。
