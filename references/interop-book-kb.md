# 与 book-kb-builder 协作：谁干什么，怎么交接

两个 skill 是上下游关系，**不要互相抢活**：

| | pdf-toolkit（本 skill） | book-kb-builder |
|---|---|---|
| 负责 | 把 PDF 变成可读、可查、可核对的形式 | 读、分类、写总结、防编造校验、落库 |
| 工具 | `pdftool.py` | `booktool.py` / `kbtool.py` / `verify.py` |
| 产出 | 可搜索 PDF、AI 友好包、页锚全文、表格/图片 | 知识库目录 + `<书名>_阅读总结.txt` |

**分工的硬边界**：本 skill 不建知识库、不写阅读总结、不做防编造判定；
book-kb-builder 不负责加 OCR 文字层。需要哪一半就调哪个 skill。

## 页锚是同一种东西

两个 skill 都用 `[[p.N]]`，N 都是 **PDF 物理页号**。所以：

- 本工具产出的 `.pages.txt` 可以直接当 `verify.py --source`；
- 本工具 `to-ai` 写出的 Markdown 里的 `[[p.N]]` 与 booktool 的 `probe` 产出语义一致；
- 印刷页码偏移（`--offset`）只在 book-kb-builder 侧记录，本工具不猜。

## 流程 A：这本书有文字层

```bash
# 1) 先看这本书是什么状态
<py> scripts/pdftool.py info "<书.pdf>"

# 2) 打 AI 友好包（同时得到可核对的页锚全文）
<py> scripts/pdftool.py to-ai "<书.pdf>" --out "<工作目录>" --images --tables

# 3) 交给 book-kb-builder：书用原 PDF，--source 用本工具产出的 pages.txt
<bookpy> scripts/booktool.py probe "<书.pdf>" --work .bookwork
<bookpy> scripts/verify.py --summary "<总结.txt>" --source "<工作目录>/<书名>.pages.txt"
```

## 流程 B：这本书是扫描版（最常见）

关键一步：**先加 OCR 文字层，再入库**。加完之后 booktool 会把它判成
`text-rich`/`text-partial`，于是这本书就能走文本路线——引文可被机器核对，
不必全程靠看页面图。

```bash
# 1) 确认确实是扫描版（文字层: scanned / text-garbled）
<py> scripts/pdftool.py info "<书.pdf>"

# 2) 加 OCR：产出可搜索 PDF + 整册页锚全文
<py> scripts/pdftool.py ocr "<书.pdf>" --out "<书_ocr.pdf>" --sidecar "<书.pages.txt>"

# 3) 入库时把 _ocr.pdf 当作"这本书"（原件仍然保留在知识库里）
<bookpy> scripts/booktool.py probe "<书_ocr.pdf>" --work .bookwork
<bookpy> scripts/booktool.py outline "<书_ocr.pdf>" --chapters
<bookpy> scripts/booktool.py search "<书_ocr.pdf>" --pattern 关键词

# 4) 校验时 source 用 OCR 产出的页锚全文
<bookpy> scripts/verify.py --summary "<总结.txt>" --source "<书.pages.txt>"
```

入库时 `kbtool.py build --book "<书_ocr.pdf>"` 会把可搜索版本复制进知识库。
要不要同时留原件（那份纯扫描件）由你判断：原件体积小一点、但没有文字层；
建议**只入库 `_ocr.pdf`**，并在总结的「图书概况」里写明"文字层由 OCR 生成"。

## OCR 文字当 `--source` 时的三条注意事项

1. **引文核对失败 ≠ 编造。** OCR 会有形近字错误（橡/様、券/卷）。
   `verify.py` 报"引文在原文中找不到"时，先回原页面图核对一眼：
   `pdftool.py render "<原书.pdf>" --pages N --dpi 300`，
   是 OCR 认错就修 OCR 文本（重跑该页、提高 `--dpi`），不要直接删引文。
2. **数字与专有名词必须回图核对。** 表格、年代、尺寸、人名标题最容易被 OCR 认错，
   而知识库里的这类信息一旦错了最难发现。
3. **覆盖度声明要写清文字层来源。** 例如"本书文字层由 OCR 生成（平均置信度 0.98），
   数字类内容已抽样回图核对"。这与 book-kb-builder「覆盖度诚实」的要求是一条心。

## 视觉路线的配合

扫描版里那些**只有图、没有文字**的页（`manifest.json` 里 `source: "none"`），
book-kb-builder 会用 `render` 出图视觉判读。本工具能替它省事的地方：

- `pdftool.py render --pages N --dpi 300`：同一件事，参数更好记；
- `pdftool.py images --render-pages --dpi 200`：批量出整页图（或只出某几页）；
- `pdftool.py images --out DIR`：把书里的插图/图版**按页号命名**导出来，
  这样总结里可以写 `(p.188·图)` 并直接对上文件。

## 别做的事

- 不要用本工具去写阅读总结或建知识库——那是 book-kb-builder 的活，
  它有防编造校验闸门，绕过去等于把质量保证丢了。
- 不要为了"看起来有文字层"而伪造文本：`to-ai` 对没有文字的页只写占位说明，
  这是有意的设计。宁可写"该页无文字层"，也不要让模型凭空补一段"看上去像书里的话"。
