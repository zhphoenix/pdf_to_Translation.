# Unlimited-OCR 扫描版 PDF 转 Markdown 项目设计规范
https://github.com/baidu/Unlimited-OCR
> Version: v2.0
>
> 更新时间：2026-07
>
> 项目名称：Unlimited-OCR-Markdown
>
> 推理框架：vLLM + NVFP4 量化模型
>
> 目标：
>
> **将扫描版 PDF、杂志、书籍、图片型 PDF 高质量转换成 Markdown 文本。**

---

# 一、项目目标

本项目仅负责 OCR，不涉及：

- RAG
- 向量数据库
- Embedding
- Agent
- Docling

定位为一个轻量级 OCR 工具。

输入：

```
PDF
```

输出：

```
Markdown (.md)
```

支持：

- 扫描版 PDF
- 杂志
- 书籍
- 图片 PDF
- PNG
- JPG
- TIFF

---

# 二、总体架构

```
                PDF

                 │

                 ▼

     PDF → Images（内存，不写文件）

                 │

                 ▼

     Unlimited-OCR (vLLM)
     输出: <|det|>type [bbox]<|/det|>text

                 │

                 ▼

      结构化解析 → OCRElement(type, bbox, text, page)

                 │

                 ▼

        后处理（过滤/合并）

                 │

                 ▼

          Final Markdown
```

整个流程只有四个步骤。

---

# 三、项目目录

```
unlimited-ocr-markdown/

│

├── models/

├── input/

├── output/

├── logs/

├── src/

│   ├── main.py

│   ├── pdf2image.py

│   ├── ocr.py

│   ├── postprocess.py

│   ├── markdown.py

│   ├── utils.py

│   └── config.py

│

├── requirements.txt

└── README.md
```

---

# 四、模块说明

## 4.1 pdf2image.py

负责：

```
PDF

↓

PNG
```

推荐：

```
PyMuPDF
```

示例：

```
book.pdf

↓

page_0001.png

page_0002.png

...

page_0300.png
```

---

## 4.2 ocr.py

负责：

调用 Unlimited-OCR vLLM API。

部署方案：

```
vLLM (Docker)
镜像: vllm/vllm-openai:unlimited-ocr
模型: sahilchachra/Unlimited-OCR-NVFP4
本地路径: /mnt/g/models/OCR/Unlimited-OCR-NVFP4
served-model-name: unlimited-ocr
端口: 8000
```

统一调用：

```
OpenAI Compatible API
```

接口：

```
POST /v1/chat/completions
```

输入：

```
图片 (base64)
```

输出：

```
<|det|>type [x1, y1, x2, y2]<|/det|>text
```

解析为：

```
OCRElement(type, bbox, text, page)
```

关键参数：

```
temperature: 0
max_tokens: 16384
skip_special_tokens: False
vllm_xargs: {ngram_size: 35, window_size: 128}
```

---

## 4.3 postprocess.py

整个项目最重要的模块。

负责恢复文章结构。

包括：

### 合并断行

例如：

```
The world

economy
```

恢复：

```
The world economy
```

---

### 去掉断词

例如：

```
inter-

national
```

恢复：

```
international
```

---

### 删除页眉

例如：

```
The Economist
```

每页重复出现。

全部删除。

---

### 删除页脚

例如：

```
July 2026
```

全部删除。

---

### 删除页码

例如：

```
43
```

全部删除。

---

### 删除广告

例如：

```
Subscribe now

Advertisement
```

删除。

---

### 保留真正段落

例如：

```
Paragraph

Paragraph

Paragraph
```

不要全部连成一段。

---

## 4.4 markdown.py

负责输出标准 Markdown。

例如：

```
# 标题

正文

正文

## 小标题

正文

> 引用

- 列表

| 表格 |
```

尽量保持原始结构。

---

## 4.5 main.py

负责整个流水线。

```
读取 PDF

↓

PDF 转图片

↓

OCR

↓

后处理

↓

Markdown

↓

保存
```

---

# 五、OCR Prompt

官方推荐 Prompt（vLLM 必须加 `<image>` 前缀）：

单页：

```
<image>document parsing.
```

多页：

```
<image>Multi page parsing.
```

注意：

- Prompt 必须以 `<image>` 开头，否则模型返回空输出
- 必须设置 `skip_special_tokens=False`，否则 `<|det|>` 标记被过滤
- 必须注册 NGramPerReqLogitsProcessor，否则长文档循环输出

---

# 六、Markdown 输出规范

推荐输出：

```
# The Next Recession

The global economy is entering a new phase.

Inflation has eased.

Interest rates remain elevated.

## Europe

European growth remains weak.

## America

Consumer demand remains resilient.

> Figure:
> GDP Growth

- Item 1
- Item 2

| Country | Growth |
|---------|--------|
| USA | 2.1% |
| China | 4.8% |
```

---

# 七、图片处理流程

```
PDF

↓

PyMuPDF

↓

PNG

↓

Unlimited-OCR

↓

Markdown

↓

Markdown Cleaner

↓

article.md
```

---

# 八、一本书处理

例如：

```
300 页
```

建议输出：

```
book.md
```

而不是：

```
300 个 Markdown
```

页面之间保留：

```
---

Page 23

---
```

方便定位。

---

# 九、批量处理

输入：

```
input/

    Economist.pdf

    Fortune.pdf

    Book.pdf

    Magazine.pdf
```

输出：

```
output/

    Economist.md

    Fortune.md

    Book.md

    Magazine.md
```

---

# 十、翻译模块（可选，默认禁用）

当前流水线不执行翻译。

翻译模块保留为可选配置：

```
translation:
  enabled: false
```

设为 true 时：

```
OCR 结构化元素

↓

按类型选择性翻译

↓

翻译后元素
```

说明：

翻译模块不负责 OCR。

只负责：

```
text 元素 → 翻译后 text 元素
```

因此速度很快。

---

# 十一、推荐项目流程

```
                 PDF

                  │

                  ▼

      PyMuPDF（内存转图片）

                  │

                  ▼

     Unlimited-OCR (vLLM)
     输出: <|det|>type [bbox]<|/det|>text

                  │

                  ▼

      解析为 OCRElement(type, bbox, text, page)

                  │

                  ▼

       后处理（结构化过滤/合并）

          ├── 去页眉
          ├── 去页脚
          ├── 去页码
          ├── 去广告
          ├── 合并相邻文本块
          ├── 保留标题
          ├── 保留引用
          ├── 保留列表
          └── 保留表格

                  │

                  ▼

           翻译（可选，默认禁用）

                  │

                  ▼

            Final Markdown

                  │

                  ▼

              article.md
```

---

# 十二、推荐技术栈

| 模块 | 推荐方案 |
|------|----------|
| PDF 解析 | PyMuPDF |
| OCR 模型 | Unlimited-OCR (NVFP4) |
| 推理框架 | vLLM (Docker) |
| Docker 镜像 | vllm/vllm-openai:unlimited-ocr |
| API | OpenAI Compatible API |
| 后处理 | Python + Regex（结构化元素过滤/合并） |
| 配置 | YAML |

---

# 十三、输出规范

每本书：

```
Book.md
```

每本杂志：

```
Economist.md
```

保持：

- 原始语言
- 原始标题
- 原始段落
- 原始阅读顺序
- Markdown 格式

---

# 十四、项目特点

✅ 不依赖数据库

✅ 不依赖 RAG

✅ 不依赖 Agent

✅ 不依赖 Docling

✅ 可本地离线运行

✅ 支持批量 PDF

✅ 支持扫描版杂志

✅ 支持扫描版书籍

✅ 支持图片型 PDF

✅ 输出高质量 Markdown

---

# 十五、未来可扩展方向

后续可增加：

```
OCR Provider

├── Unlimited-OCR
├── DeepSeek-OCR
├── PaddleOCR
├── MinerU OCR
└── Future OCR Models
```

统一接口：

```
Image

↓

OCR Provider

↓

Markdown
```

方便后续升级 OCR 引擎。

---

# 十六、最佳实践

推荐最终流水线：

```
                    PDF

                     │

                     ▼

        PyMuPDF（内存转图片）

                     │

                     ▼

        Unlimited-OCR (vLLM)
        输出: <|det|>type [bbox]<|/det|>text

                     │

                     ▼

        解析为 OCRElement(type, bbox, text, page)

                     │

                     ▼

          后处理（结构化过滤/合并）

                     │

         ├── 去页眉页脚
         ├── 去页码
         ├── 去广告
         ├── 合并相邻文本块
         ├── 保留标题
         ├── 保留引用
         ├── 保留列表
         └── 保留表格

                     │

                     ▼

           翻译（可选，默认禁用）

                     │

                     ▼

              Final Markdown

                     │

                     ▼

                article.md
```

---

# 十七、项目总结

本项目专注于 **扫描版 PDF → Markdown** 的高质量转换。

设计原则：

- **简单**：仅保留 OCR 所需模块，不引入数据库、RAG 或 Agent。
- **准确**：通过合理的 OCR Prompt 和后处理，尽可能恢复原始文章结构。
- **可维护**：模块职责单一，便于替换 OCR 引擎或扩展功能。
- **可扩展**：未来可接入更多 OCR Provider，而无需修改整体流程。

推荐用于：

- 《经济学人》扫描版
- 商业杂志
- 扫描书籍
- 历史文献
- 图片型 PDF
- 档案数字化项目