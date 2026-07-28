# Unlimited-OCR 扫描版 PDF 转 Markdown 项目设计规范

> Version: v3.0
>
> 更新时间：2026-07-28
>
> 项目名称：Unlimited-OCR-Markdown
>
> 推理框架：vLLM + NVFP4 量化模型
>
> 上游项目：https://github.com/baidu/Unlimited-OCR

---

## 一、项目目标

**将扫描版 PDF、杂志、书籍、图片型 PDF 高质量转换成 Markdown 文本。**

定位为一个**轻量级 OCR 工具**，不涉及：

- RAG / 向量数据库 / Embedding
- Agent / Docling
- 任何云端依赖

支持输入：

- 扫描版 PDF、杂志、书籍、图片 PDF
- PNG / JPG / TIFF 图片

输出：

- 结构化 Markdown（`.md`）
- 可选翻译纯文本（`.txt`）
- 中间结构化 JSON（`.ocr.json`，分步模式）

---

## 二、总体架构

```
PDF 输入
    │
    ▼
PyMuPDF 渲染（内存，JPEG 编码，不写中间文件）
    │
    ▼
页面预检（page_filter：跳过图片页/广告页）
    │
    ▼
并发 OCR（ThreadPoolExecutor + vLLM continuous batching）
输出: <|det|>type [bbox]<|/det|>text
    │
    ▼
结构化解析 → OCRElement(type, bbox, text, page)
    │
    ▼
后处理（过滤页眉/页脚/页码/广告 + 合并断行）
    │
    ▼
翻译（可选，llama.cpp 本地 LLM）
    │
    ▼
Markdown 生成 → article.md
```

核心设计原则：

- **内存模式**：PDF 渲染为图片字节流，不写磁盘中间文件
- **分步执行**：OCR 与翻译串行（16GB 显存不足同时运行两个模型）
- **并发加速**：客户端多路请求利用 vLLM continuous batching
- **断点续传**：已有 `.ocr.json` 的文件自动跳过

---

## 三、项目目录

```
unlimited-ocr-markdown/
├── input/                  # PDF 输入目录
├── output/                 # 输出目录（.ocr.json / .md / .txt）
├── logs/                   # 运行日志
├── models/                 # 模型目录（占位）
├── src/
│   ├── __init__.py
│   ├── main.py             # 主流水线（完整模式 + 分步模式 + 并发 OCR）
│   ├── ocr.py              # OCR 引擎（vLLM OpenAI Compatible API）
│   ├── pdf2image.py        # PDF 转图片（PyMuPDF，内存模式）
│   ├── page_filter.py      # 页面预检（图片页/广告页跳过）
│   ├── postprocess.py      # 结构化后处理（过滤/合并）
│   ├── markdown.py         # Markdown 生成
│   ├── translate.py        # 翻译模块（可选，llama.cpp API）
│   ├── config.py           # 配置加载（YAML 单例）
│   └── utils.py            # 工具函数（日志/进度条/编码）
├── config.yaml             # 配置文件
├── docker-compose.yml      # 服务编排（OCR + 翻译 profiles 隔离）
├── run_pipeline.sh         # 两阶段自动化编排脚本
├── requirements.txt        # Python 依赖
└── README.md               # 项目说明
```

---

## 四、模块职责

### 4.1 main.py — 主流水线

职责：整个流水线的入口与调度。

支持两种运行模式：

| 模式 | 触发方式 | 流程 |
|------|----------|------|
| 完整模式 | `python -m src.main input/` | PDF → OCR → 后处理 → 翻译(可选) → Markdown |
| 分步模式 | `--step ocr` / `--step translate` | 拆分为两个独立步骤，中间通过 `.ocr.json` 衔接 |

分步模式 OCR 阶段三阶段流程：

```
第一阶段：分类页面
    page_filter 预检 → 图片页标记跳过 / 内容页加入 OCR 队列

第二阶段：预渲染 + 并发 API
    串行渲染所有页面（JPEG 编码，CPU 密集但很快）
    → ThreadPoolExecutor 并发发送 API 请求（I/O 密集）

第三阶段：后处理 + 组装
    按页序执行后处理 → 低价值页标记 → 保存 .ocr.json
```

关键函数：

- `process_single_pdf()` — 完整模式处理单个 PDF
- `step_ocr()` — 分步模式第一步（并发 OCR + 后处理 → JSON）
- `step_translate()` — 分步模式第二步（读取 JSON → 翻译 → 输出）
- `_ensure_translation_stopped()` — OCR 前自动检查并停止翻译容器

### 4.2 pdf2image.py — PDF 转图片

职责：使用 PyMuPDF 将 PDF 页面渲染为图片。

两种模式：

| 函数 | 用途 | 输出 |
|------|------|------|
| `pdf_to_images()` | 批量导出到磁盘 | PNG 文件列表 |
| `extract_page_image()` | 单页内存渲染（主流水线用） | JPEG/PNG 字节 |

关键实现：

- 默认 OCR 传输格式为 JPEG（`pdf.ocr_format: "jpeg"`），编码速度比 PNG 快 3-5x，体积小 70%
- `alpha=False` 减少内存占用
- JPEG quality=95 保留文字细节

### 4.3 ocr.py — OCR 引擎

职责：调用 Unlimited-OCR vLLM API，解析结构化输出。

部署方案：

```
推理框架: vLLM (Docker)
镜像:     vllm/vllm-openai:unlimited-ocr
模型:     sahilchachra/Unlimited-OCR-NVFP4
本地路径: /mnt/g/models/OCR/Unlimited-OCR-NVFP4
端口:     8000
API:      OpenAI Compatible (POST /v1/chat/completions)
```

OCR Prompt（vLLM 必须以 `<image>` 开头）：

```
单页: <image>document parsing.
多页: <image>Multi page parsing.
```

API 调用参数：

```python
temperature=0
max_tokens=16384
skip_special_tokens=False       # 保留 <|det|> 标记
vllm_xargs={
    "ngram_size": 35,           # 防重复 n-gram
    "window_size": 128/1024     # 单页/多页窗口
}
```

输出格式解析：

```
<|det|>title [37, 64, 464, 132]<|/det|>INVOICE #2026-0623
<|det|>text [37, 194, 350, 247]<|/det|>Bill To: Sahil Chachra
```

解析为 `OCRElement(type, bbox, text, page)` 数据结构。

兜底策略：

1. 优先匹配 vLLM `<|det|>` 格式
2. 回退 llama.cpp 旧格式（`type [bbox]text`）
3. 仍失败 → 非空行保留为 `type="text", bbox=(0,0,0,0)`
4. 整页解析为空 → 自动用 MULTI prompt 重试一次

### 4.4 page_filter.py — 页面预检

职责：OCR 前判断页面是否为图片页（广告/照片/插图），跳过无意义的 GPU 调用。

分层决策逻辑：

```
1. text_chars >= 1500 → 内容页（强保护，早退跳过 pixmap 渲染）
2. 图片覆盖 >= 85% + 颜色少 → 图形广告
3. 图片覆盖 >= 85% + 颜色多 → 照片广告
4. 文字 < 50 + 图片覆盖 >= 60% → 图片页
5. 文字 < 50 + 颜色丰富 + 墨迹适中 → 全页照片
6. 其余 → 保留（正常内容页）
```

性能优化：

- 内容页（文字量充足）直接早退，跳过昂贵的 pixmap 渲染和颜色分析
- 颜色分析使用低 DPI（72）渲染 + 降采样（12000 像素），内存开销极小

### 4.5 postprocess.py — 结构化后处理

职责：基于 OCR 结构化元素恢复文章逻辑结构。

处理步骤：

| 步骤 | 说明 |
|------|------|
| 按类型过滤 | 删除 `header`、`footer`、`page_number` 类型元素 |
| 删除广告 | 匹配 `ad_patterns` 关键词 |
| 自定义过滤 | 用户定义的页眉/页脚正则 |
| 跨页去重 | 出现在 >50% 页面的短文本（<80字符）判定为页眉/页脚 |
| 合并断行 | 同列（x1 差 <30px）、y 间距小、前文未以句号结尾的相邻 text 元素合并 |

### 4.6 markdown.py — Markdown 生成

职责：从结构化元素生成标准 Markdown。

元素类型映射：

| OCR type | Markdown |
|----------|----------|
| title | `# 标题` |
| heading | `## 小标题` |
| subtitle | `### 子标题` |
| caption / quote | `> 引用` |
| list_item | `- 列表项` |
| table_cell | `\| 表格 \|` |
| text | 正文段落 |

页面间插入分隔符：

```
---

Page 23

---
```

### 4.7 translate.py — 翻译模块（可选）

职责：将 OCR 结构化元素翻译为目标语言。

部署方案：

```
推理框架: llama.cpp (Docker)
镜像:     ghcr.io/ggml-org/llama.cpp:server-cuda13
模型:     unsloth/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q8_0.gguf
端口:     8080
API:      OpenAI Compatible
```

两种输出模式：

| 模式 | 函数 | 输出 |
|------|------|------|
| 结构化翻译 | `translate_pages()` | 翻译后 OCRElement → Markdown |
| 纯文本翻译 | `translate_pages_to_text()` | 直接输出译文 `.txt` |

翻译策略：

- 仅翻译 `text/title/heading/subtitle/caption/quote` 类型
- 批量翻译（`batch_size=5`），编号格式合并为单次请求
- 专业英译中 Prompt（针对《经济学人》等杂志优化）
- 低温度（0.3）保证翻译稳定性

### 4.8 config.py — 配置加载

职责：YAML 配置单例，支持点号分隔嵌套键访问。

```python
config.get("ocr.api_base")         # → "http://localhost:8000/v1"
config.get("page_filter.render_dpi")  # → 72
config.get_path("paths.output_dir")   # → 绝对路径
```

### 4.9 utils.py — 工具函数

提供：日志配置（终端+文件双输出）、进度条（tqdm）、图片 base64 编码、MIME 检测、PDF 文件列表获取、文件名清理。

---

## 五、配置说明

配置文件：`config.yaml`

### 5.1 OCR 配置

```yaml
ocr:
  api_base: "http://localhost:8000/v1"   # vLLM API 地址
  model: "unlimited-ocr"                 # 对应 --served-model-name
  timeout: 300                           # 请求超时（秒）
  max_retries: 3                         # 最大重试次数
  concurrency: 3                         # 客户端并发数（利用 vLLM continuous batching）
  skip_special_tokens: false             # 必须 false：保留 <|det|> 标记
  ngram_size: 35                         # 防重复 n-gram 大小
  ngram_window_single: 128               # 单页窗口
  ngram_window_multi: 1024               # 多页窗口
```

### 5.2 PDF 渲染配置

```yaml
pdf:
  dpi: 300              # 渲染 DPI（300 保证 OCR 质量）
  format: "png"         # 磁盘保存格式（pdf_to_images 用）
  ocr_format: "jpeg"    # OCR 传输格式（jpeg 快+小，png 无损）
```

### 5.3 页面预检配置

```yaml
page_filter:
  skip_image_pages: true          # 总开关
  min_text_chars: 50              # 文字极少阈值
  body_text_threshold: 1500       # 内容页强保护阈值
  image_coverage_threshold: 0.6   # 图片覆盖中等阈值
  high_image_coverage: 0.85       # 图片覆盖高阈值（广告页）
  color_threshold: 100            # 颜色丰富度阈值
  ink_coverage_min: 0.08          # 墨迹覆盖率下限
  ink_coverage_max: 0.75          # 墨迹覆盖率上限
  render_dpi: 72                  # 颜色分析渲染 DPI
  min_ocr_elements: 3             # OCR 后低价值页判定阈值
```

### 5.4 后处理配置

```yaml
postprocess:
  remove_headers: true
  remove_footers: true
  remove_page_numbers: true
  remove_ads: true
  merge_broken_lines: true
  merge_broken_words: true
  header_patterns: []             # 自定义页眉正则
  footer_patterns: []             # 自定义页脚正则
  ad_patterns:
    - "Subscribe now"
    - "Advertisement"
```

### 5.5 翻译配置

```yaml
translation:
  enabled: false                  # 默认关闭
  api_base: "http://localhost:8080/v1"
  model: "/models/unsloth/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q8_0.gguf"
  target_language: "简体中文"
  timeout: 300
  batch_size: 5                   # 每次翻译的元素数
  max_tokens: 8192
  temperature: 0.3
```

### 5.6 输出与路径配置

```yaml
output:
  page_separator: true            # 页面间插入 --- Page N ---
  single_file: true               # 整本书输出为单个 .md

paths:
  input_dir: "input"
  output_dir: "output"
  log_dir: "logs"
```

---

## 六、Docker 服务编排

### 6.1 设计约束

RTX 5060 Ti 16GB 显存不足以同时运行 OCR 模型（~15GB）和翻译模型（~10GB）。
使用 Docker Compose **profiles** 隔离两个服务，串行调度。

### 6.2 服务定义

| 服务 | 容器名 | Profile | 端口 | 框架 | 模型 |
|------|--------|---------|------|------|------|
| unlimited-ocr | unlimited-ocr | `ocr` | 8000 | vLLM | Unlimited-OCR-NVFP4 |
| sisyphus | sisyphus | `translate` | 8080 | llama.cpp | Qwythos-9B-Q8_0 |

### 6.3 vLLM OCR 启动参数

```yaml
command:
  - /model
  - --served-model-name unlimited-ocr
  - --trust-remote-code
  - --logits_processors "vllm.model_executor.models.unlimited_ocr:NGramPerReqLogitsProcessor"
  - --no-enable-prefix-caching
  - --mm-processor-cache-gb 0
  - --max-model-len 32768
```

关键说明：

- `NGramPerReqLogitsProcessor`：防止长文档循环输出（必须）
- `--no-enable-prefix-caching`：多模态模型不适用前缀缓存
- `--mm-processor-cache-gb 0`：禁用多模态处理器缓存（节省显存）
- `--max-model-len 32768`：KV cache 预分配上限

### 6.4 llama.cpp 翻译启动参数

```yaml
command:
  - -m /models/unsloth/Qwythos-9B-Claude-Mythos-5-1M-MTP-Q8_0.gguf
  - --flash-attn on
  - --cache-type-k q4_0
  - --cache-type-v q4_0
  - -ngl 99
  - -c 65536
  - --spec-type draft-mtp
  - --spec-draft-n-max 6
```

---

## 七、两阶段流水线

### 7.1 设计原因

OCR 模型（vLLM + NVFP4）占用 ~15GB 显存，翻译模型（llama.cpp Q8_0）占用 ~10GB。
16GB 显卡无法同时承载，必须串行执行。

### 7.2 执行流程

```
┌─────────────────────────────────────────────────┐
│ 阶段 1: OCR                                      │
│                                                   │
│  停止翻译容器 → 启动 OCR 容器 → 等待就绪          │
│  → 执行 OCR（并发）→ 保存 .ocr.json              │
│  → 停止 OCR 容器 → 等待显存释放                   │
├─────────────────────────────────────────────────┤
│ 阶段 2: 翻译                                     │
│                                                   │
│  启动翻译容器 → 等待就绪                          │
│  → 读取 .ocr.json → 翻译 → 输出 .md / .txt      │
│  → 停止翻译容器                                   │
└─────────────────────────────────────────────────┘
```

### 7.3 自动化编排

`run_pipeline.sh` 实现全自动调度：

```bash
./run_pipeline.sh input/              # 处理目录（默认 Markdown 输出）
./run_pipeline.sh input/book.pdf      # 处理单个 PDF
./run_pipeline.sh input/ --text       # 翻译输出纯文本
./run_pipeline.sh input/ --skip-ocr   # 跳过 OCR（已有 JSON 直接翻译）
```

### 7.4 安全保障

- OCR 前自动检查翻译容器是否运行，若是则先停止（`_ensure_translation_stopped()`）
- 容器停止后等待 3-5 秒确保 GPU 显存完全释放
- 服务就绪检查：轮询 `/v1/models` 端点，超时 120 秒报错

---

## 八、并发 OCR

### 8.1 设计原理

vLLM 支持 continuous batching：多个客户端请求同时到达时，GPU 内部自动批处理，
吞吐量随并发数线性增长（KV cache 已预分配，不增加显存）。

### 8.2 实现方案：预渲染 + 并发 API 分离

```
串行阶段（CPU 密集）：
    所有待 OCR 页面 → extract_page_image() → JPEG bytes 缓存

并发阶段（I/O 密集）：
    ThreadPoolExecutor(max_workers=concurrency)
    → 每线程仅执行 ocr_image_bytes()（纯 API 调用）
    → vLLM 同时收到多路请求 → continuous batching 生效
```

**为什么分离？**

如果渲染和 API 在同一线程，PNG/JPEG 编码耗时 1-3 秒导致请求无法同时到达 vLLM，
实际退化为串行（vLLM 日志显示 `Running: 1 req`）。分离后 vLLM 稳定 `Running: 3 reqs`。

### 8.3 性能数据

| 指标 | 串行 | 并发 (concurrency=3) |
|------|------|---------------------|
| 10 页 PDF 总耗时 | 105.8s | **44.8s** (-57.7%) |
| 有效 OCR 页均耗时 | 17.6s | **7.5s** |
| vLLM 吞吐 | 200 tok/s | **444 tok/s** (+122%) |
| GPU 显存峰值 | 14979 MiB | 14989 MiB（无变化） |
| KV cache 占用 | 1.1% | 3.2%（安全） |

### 8.4 配置与回滚

```yaml
# 启用并发（推荐）
ocr:
  concurrency: 3
pdf:
  ocr_format: "jpeg"

# 回滚为串行
ocr:
  concurrency: 1
pdf:
  ocr_format: "png"
```

---

## 九、图片页跳过

### 9.1 目的

杂志中大量全页广告、照片、插图页面无需 OCR。跳过这些页面可节省 ~40% GPU 时间。

### 9.2 判断逻辑

采用分层决策，优先保护内容页：

```
文字层字符数 >= 1500 → 必为内容页（强保护，跳过后续分析）
文字稀疏 + 图片覆盖 >= 85% → 广告页（按颜色区分图形/照片广告）
文字极少(< 50) + 图片覆盖 >= 60% → 图片页
文字极少 + 颜色丰富 + 墨迹适中 → 全页照片
其余 → 保留
```

### 9.3 后置检查

OCR 完成后，若某页有效文本元素 < 3 个，标记为"低价值页"（`low-value`），
替换为占位元素 `[图片页 - 已跳过]`。

### 9.4 JSON 记录

跳过页在 `.ocr.json` 中保留记录：

```json
{
  "page": 5,
  "skipped": true,
  "reason": "image-only page (photo-ad (coverage 92%, colors 245))",
  "elements": [{"type": "text", "bbox": [0,0,0,0], "text": "[图片页 - 已跳过]（...）"}]
}
```

---

## 十、运行命令

### 10.1 服务管理

```bash
# OCR 服务
docker compose --profile ocr up -d unlimited-ocr
docker compose --profile ocr stop unlimited-ocr
docker logs -f unlimited-ocr

# 翻译服务
docker compose --profile translate up -d sisyphus
docker compose --profile translate stop sisyphus
docker logs -f sisyphus

# 通用
docker ps
docker compose down
```

### 10.2 主流水线

```bash
# 完整流程
python -m src.main input/book.pdf                 # 单个 PDF
python -m src.main input/                         # 批量
python -m src.main input/ --translate             # 启用翻译
python -m src.main input/ --translate-only        # 翻译输出纯文本
python -m src.main input/ --no-translate          # 禁用翻译
python -m src.main input/ --dpi 200               # 指定 DPI
python -m src.main input/ -o output/ -v           # 指定输出目录 + 详细日志

# 分步模式
python -m src.main input/ --step ocr              # 第一步：OCR → .ocr.json
python -m src.main output/ --step translate       # 第二步：翻译 → .md
python -m src.main output/ --step translate --translate-only  # 翻译 → .txt
```

### 10.3 自动化编排

```bash
./run_pipeline.sh input/                # 一键全自动（OCR + 翻译）
./run_pipeline.sh input/ --text         # 翻译输出纯文本
./run_pipeline.sh input/ --skip-ocr     # 跳过 OCR 直接翻译
```

### 10.4 健康检查

```bash
curl http://localhost:8000/health       # OCR 服务
curl http://localhost:8000/v1/models    # OCR 模型列表
curl http://localhost:8080/health       # 翻译服务
nvidia-smi                              # GPU 显存状态
```

---

## 十一、输出规范

### 11.1 文件命名

| 模式 | 输出文件 |
|------|----------|
| OCR 分步 | `{pdf_name}.ocr.json` |
| Markdown | `{pdf_name}.md` |
| 翻译纯文本 | `{pdf_name}_translated.txt` |

### 11.2 Markdown 格式

```markdown
---

Page 1

---

# The Next Recession

The global economy is entering a new phase.

Inflation has eased.

## Europe

European growth remains weak.

> Figure: GDP Growth

- Item 1
- Item 2

| Country | Growth |
|---------|--------|
| USA | 2.1% |
```

### 11.3 中间 JSON 格式

```json
{
  "source": "/path/to/input.pdf",
  "total_pages": 80,
  "pages": [
    {
      "page": 1,
      "skipped": false,
      "reason": "",
      "elements": [
        {"type": "title", "bbox": [37, 64, 464, 132], "text": "...", "page": 1},
        {"type": "text", "bbox": [37, 194, 350, 247], "text": "...", "page": 1}
      ]
    }
  ]
}
```

### 11.4 输出原则

- 保持原始语言（不翻译时）
- 保持原始标题、段落、阅读顺序
- 整本书/杂志输出为单个文件
- 页面间保留 `--- Page N ---` 分隔符方便定位

---

## 十二、技术栈

| 模块 | 方案 |
|------|------|
| PDF 解析 | PyMuPDF (fitz) |
| OCR 模型 | Unlimited-OCR (NVFP4, ~2.93GB) |
| OCR 推理 | vLLM (Docker, OpenAI Compatible API) |
| 翻译模型 | Qwythos-9B-Claude-Mythos (Q8_0 GGUF) |
| 翻译推理 | llama.cpp (Docker, CUDA) |
| 并发 | ThreadPoolExecutor + vLLM continuous batching |
| 后处理 | Python + Regex（结构化元素过滤/合并） |
| 配置 | PyYAML |
| 进度 | tqdm |
| API 客户端 | openai (Python SDK) |

Python 依赖：

```
PyMuPDF>=1.24.0
Pillow>=10.0.0
openai>=1.0.0
PyYAML>=6.0
tqdm>=4.60.0
```

---

## 十三、最佳实践

### 13.1 推荐生产配置

```yaml
ocr:
  concurrency: 3          # RTX 5060 Ti 16GB 安全值
  timeout: 300
pdf:
  dpi: 300                # 保证 OCR 质量
  ocr_format: "jpeg"     # 速度优先
page_filter:
  skip_image_pages: true  # 跳过广告页节省 ~40% GPU 时间
```

### 13.2 大文件处理策略

80+ 页杂志/书籍推荐分步模式：

```bash
# 1. 仅 OCR（可中断，已有 JSON 自动跳过）
python -m src.main input/ --step ocr

# 2. 检查中间结果
ls output/*.ocr.json

# 3. 翻译
python -m src.main output/ --step translate
```

优势：

- OCR 阶段可中断恢复（断点续传）
- 两阶段独立，不受显存限制
- 可单独重跑翻译（调整 Prompt 后无需重新 OCR）

### 13.3 显存安全

- OCR 与翻译**绝不**同时运行
- `run_pipeline.sh` 自动管理容器启停
- 手动操作时务必先停一个再启另一个
- concurrency=3 时 KV cache 仅用 3.2%，可安全提升至 4

### 13.4 质量保障

- DPI 保持 300（ViT 模型内部下采样，降低 DPI 收益极小但有质量风险）
- JPEG quality=95（元素数差异 <7%，视觉模型正常波动范围内）
- `skip_special_tokens: false`（必须，否则 `<|det|>` 标记丢失）
- NGramPerReqLogitsProcessor（必须，否则长文档循环输出）

---

## 十四、项目特点

- 不依赖数据库 / RAG / Agent / Docling
- 全流程本地离线运行
- 支持批量 PDF 处理
- 支持扫描版杂志、书籍、图片型 PDF
- 输出高质量结构化 Markdown
- 内存模式无中间文件（PDF → 图片字节 → API）
- 并发 OCR 充分利用 GPU（提速 57.7%）
- 图片页智能跳过（节省 ~40% GPU 时间）
- 分步模式支持断点续传
- 两阶段串行解决 16GB 显存限制

---

## 十五、扩展方向

### 15.1 OCR Provider 抽象

```
Image → OCR Provider → OCRElement[]
              ├── Unlimited-OCR (当前)
              ├── PaddleOCR
              ├── MinerU OCR
              └── Future Models
```

统一接口，方便后续升级 OCR 引擎。

### 15.2 其他可扩展

- 多 GPU 支持（OCR + 翻译并行）
- 表格识别增强（专用表格模型）
- 输出格式扩展（HTML / DOCX / EPUB）
- Web UI（批量上传 + 进度监控）
- 语言自动检测 + 多语言翻译

---

## 十六、设计原则总结

- **简单**：仅保留 OCR 所需模块，不引入数据库、RAG 或 Agent
- **准确**：合理的 OCR Prompt + 结构化后处理，恢复原始文章结构
- **高效**：并发 OCR + 图片页跳过 + JPEG 编码，最大化 GPU 利用率
- **安全**：16GB 显存串行策略 + 自动容器管理 + 断点续传
- **可维护**：模块职责单一，配置驱动，便于替换 OCR 引擎或扩展功能

推荐用于：

- 《经济学人》扫描版
- 商业杂志（Fortune、Bloomberg 等）
- 扫描书籍
- 历史文献
- 图片型 PDF
- 档案数字化项目
