# Unlimited-OCR 扫描版 PDF 转 Markdown 项目设计规范

> Version: v3.2
>
> 更新时间：2026-07-28
>
> 项目名称：Unlimited-OCR-Markdown
>
> 推理框架：vLLM + NVFP4 量化模型
>
> 翻译模型：Qwythos-9B-Claude-Mythos（推理模型，由 ai-platform 独立管理）
>
> 上游项目：https://github.com/baidu/Unlimited-OCR

---

## 一、项目目标

**将扫描版 PDF、杂志、书籍、图片型 PDF 高质量转换成 Markdown 文本，可选英译中翻译。**

定位为轻量级本地 OCR 工具，不依赖 RAG / Agent / 云端服务。

- **输入**：扫描版 PDF、PNG / JPG / TIFF 图片
- **输出**：结构化 Markdown（`.md`）、翻译纯文本（`.txt`）、中间 JSON（`.ocr.json`）

---

## 二、总体架构

```
PDF 输入
    │
    ▼
PyMuPDF 渲染（内存 JPEG，不写磁盘）
    │
    ▼
页面预检（跳过图片页/广告页）
    │
    ▼
分批并发 OCR（每 10 批，渲染→OCR→后处理→保存→释放）
输出: <|det|>type [bbox]<|/det|>text
    │
    ▼
结构化解析 → OCRElement(type, bbox, text, page)
    │
    ▼
质量过滤（重复退化检测 + 幻觉识别 + 元素去重）
    │
    ▼
后处理（过滤页眉/页脚/页码/广告 + 合并断行）
    │
    ▼
翻译（可选，本地推理模型，关闭 thinking 提速 60x）
    │  批量翻译 + 多格式解析 + 三级容错 + OCR 重复主动清理
    ▼
Markdown 生成 → article.md
```

核心设计原则：

- **内存模式**：PDF 渲染为字节流，不写磁盘中间文件
- **分批处理**：每 10 页一批，保存中间结果并释放内存
- **质量过滤**：n-gram 唯一比 + 幻觉检测 + 元素级去重 + 句级重复截断
- **串行执行**：OCR 与翻译分时运行（显存不足以同时承载）
- **翻译容错**：三级降级（批量→逐条→保留原文）+ 断点续传
- **OCR 重复清理**：翻译 Prompt 指令 + 后处理去重双管齐下

---

## 三、项目目录

```
OCR/                            # 本项目
├── input/                      # PDF 输入
├── output/                     # 输出（.ocr.json / .md / .txt / .interim.md）
├── logs/                       # 运行日志
├── src/
│   ├── main.py                 # 主流水线（完整模式 + 分步模式 + 分批并发）
│   ├── ocr.py                  # OCR 引擎（vLLM OpenAI Compatible API）
│   ├── pdf2image.py            # PDF 转图片（PyMuPDF 内存模式）
│   ├── page_filter.py          # 页面预检（图片页跳过）
│   ├── postprocess.py          # 后处理（质量过滤 + 去重 + 过滤/合并）
│   ├── markdown.py             # Markdown 生成
│   ├── translate.py            # 翻译模块（推理模型，支持 disable_thinking）
│   ├── config.py               # 配置加载（YAML + .env 环境变量替换）
│   └── utils.py                # 工具函数（日志/进度条/编码）
├── .env                        # 环境变量（不提交 git）
├── config.yaml                 # 配置文件
├── docker-compose.yml          # 仅管理 OCR 服务
└── requirements.txt

ai-platform/sisyphus/           # 翻译服务（外部独立项目，非本项目管理）
└── compose.yml                 # sisyphus 容器编排（Qwythos-9B 推理模型）
```

---

## 四、模块职责

### 4.1 main.py — 主流水线

| 模式 | 触发方式 | 流程 |
|------|----------|------|
| 完整模式 | `python -m src.main input/` | PDF → OCR → 后处理 → 翻译(可选) → Markdown |
| 分步模式 | `--step ocr` / `--step translate` | 拆分两步，通过 `.ocr.json` 衔接 |

分步 OCR 分批流程（每 10 页）：渲染 → 并发 API → 后处理 → 保存 JSON → 释放内存。

关键函数：`process_single_pdf()`、`step_ocr()`、`step_translate()`。

### 4.2 pdf2image.py — PDF 转图片

| 函数 | 用途 | 输出 |
|------|------|------|
| `pdf_to_images()` | 批量导出到磁盘 | PNG 文件列表 |
| `extract_page_image()` | 单页内存渲染（主流水线用） | JPEG/PNG 字节 |

默认 OCR 传输格式 JPEG（编码快 3-5x，体积小 70%），quality=95。

### 4.3 ocr.py — OCR 引擎

```
推理框架: vLLM (Docker)     端口: 8000
模型:     Unlimited-OCR-NVFP4
API:      OpenAI Compatible (POST /v1/chat/completions)
```

API 参数：`temperature=0`, `max_tokens=16384`, `skip_special_tokens=False`（保留 `<|det|>` 标记）。

输出格式：`<|det|>type [bbox]<|/det|>text` → 解析为 `OCRElement(type, bbox, text, page)`。

兜底策略：`<|det|>` 格式 → llama.cpp 旧格式 → 非空行保留 → 整页空则 MULTI prompt 重试。

### 4.4 page_filter.py — 页面预检

OCR 前判断是否为图片页（广告/照片/插图），分层决策：

```
文字 >= 1500 → 内容页（强保护，早退）
图片覆盖 >= 85% + 颜色少 → 图形广告
图片覆盖 >= 85% + 颜色多 → 照片广告
文字 < 50 + 图片覆盖 >= 60% → 图片页
其余 → 保留
```

颜色分析用低 DPI（72）渲染 + 降采样，内存开销极小。

### 4.5 postprocess.py — 结构化后处理

处理步骤：

| 步骤 | 说明 |
|------|------|
| **质量过滤** | n-gram 唯一比检测重复 + 幻觉识别 + 句级重复截断（≥500 字符） |
| **元素去重** | Jaccard 3-gram 相似度 ≥ 0.85 的重复元素移除 |
| 按类型过滤 | 删除 header / footer / page_number |
| 删除广告 | 匹配 `ad_patterns` |
| 跨页去重 | 出现在 >50% 页面的短文本判定为页眉/页脚 |
| 合并断行 | 同列、y 间距小、未以句号结尾的相邻 text 合并 |

**元素级去重算法**（`_deduplicate_elements`）：对每个元素生成 3-gram 集合，与已保留元素计算 Jaccard 相似度，超过阈值（0.85）视为重复跳过。短文本（<50 字符）不参与去重。

**质量过滤流程**：

```
每个 OCR 元素:
    ├─ 幻觉前缀匹配 → 移除
    ├─ 长度 > 500 且 n-gram 唯一比 < 0.5 → 滑动窗口截断
    │     ├─ 截断后过短 → 移除
    │     └─ 截断后合理 → 保留
    └─ 正常 → 保留
```

### 4.6 markdown.py — Markdown 生成

元素映射：`title→#`, `heading→##`, `caption/quote→>`, `list_item→-`, `table_cell→|表格|`, `text→段落`。页面间插入 `--- Page N ---` 分隔符。

### 4.7 translate.py — 翻译模块（可选）

**翻译模型**：Qwythos-9B-Claude-Mythos（推理模型），由 `/mnt/e/ai-platform/sisyphus/` 独立管理。

**推理模型特性**：

- 响应包含 `reasoning_content`（思考过程）+ `content`（译文）
- 通过 `model_extra` 字段获取 `reasoning_content`
- `max_tokens` 不足时推理消耗所有 token，`content` 为空
- `_call_api()` 方法处理响应解析 + content 空时自动 2x 重试（上限 32768）

**推理关闭优化**（`disable_thinking: true`）：

通过 `chat_template_kwargs: {"enable_thinking": False}` 关闭推理思考，completion_tokens 从 ~400 降至 ~15，**提速 60x+**，译文质量无损。

| 指标 | 开启推理 | 关闭推理 |
|------|---------|---------|
| completion_tokens | ~400-900 | ~15 |
| reasoning_content | 有 | None |
| 翻译质量 | 好 | 同样好 |

**两种输出模式**：结构化翻译（OCRElement → Markdown）、纯文本翻译（`.txt`）。

**翻译策略**：

- 仅翻译 `text/title/heading/subtitle/caption/quote/image_caption/image_footnote/table`
- 批量翻译 `batch_size=3`，支持 `[N]`/`N.`/`**N.**`/`#N` 四种编号格式
- Prompt 含 `{count}` 变量确保完整性
- 补译用 `strip()` 对比避免空白误判

**三级容错**：批量翻译 → 未匹配逐条补译 → 失败保留原文。

**OCR 重复主动清理**（Prompt 指令）：

- 规则 7：原文重复句子/段落（OCR 错误），静默去除，只翻译一次
- 规则 8：高度相似内容合并为连贯译文
- **防元注释**：严格禁止 LLM 添加去重说明、括号注释或任何元评论

### 4.8 config.py — 配置加载

YAML 单例 + `.env` 环境变量替换。`${VAR_NAME}` 自动替换为 `os.environ[VAR_NAME]`，使用 `python-dotenv` 加载。

### 4.9 utils.py — 工具函数

日志（终端+文件双输出）、进度条（tqdm）、图片 base64 编码、PDF 文件列表获取。

---

## 五、配置说明

### 5.1 OCR 配置

```yaml
ocr:
  api_base: "http://localhost:8000/v1"
  model: "unlimited-ocr"
  timeout: 300
  max_retries: 3
  concurrency: 3                  # 客户端并发数（利用 vLLM continuous batching）
  skip_special_tokens: false      # 保留 <|det|> 标记
  ngram_size: 35
  ngram_window_single: 128
  ngram_window_multi: 1024
```

### 5.2 PDF 渲染配置

```yaml
pdf:
  dpi: 300              # 渲染 DPI
  format: "png"         # 磁盘保存格式
  ocr_format: "jpeg"    # OCR 传输格式（快+小）
```

### 5.3 页面预检配置

```yaml
page_filter:
  skip_image_pages: true
  min_text_chars: 50
  body_text_threshold: 1500       # 内容页强保护
  image_coverage_threshold: 0.6
  high_image_coverage: 0.85
  color_threshold: 100
  ink_coverage_min: 0.08
  ink_coverage_max: 0.75
  render_dpi: 72
  min_ocr_elements: 3
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
  header_patterns: []
  footer_patterns: []
  ad_patterns:
    - "Subscribe now"
    - "Advertisement"
  quality_filter:
    enabled: true
    repeat_min_length: 500         # 降低阈值捕获短文本重复（原 3000）
    repeat_max_ratio: 0.5
    repeat_ngram_size: 15
    sentence_repeat_threshold: 3   # 同一句子 N 次触发截断
    dedup_similarity_threshold: 0.85  # 元素去重 Jaccard 阈值
    hallucination_prefixes:
      - "The Ground Truth image displays"
      - "The image contains no text"
      - "According to Rule"
```

### 5.5 翻译配置

```yaml
# 翻译服务（本地推理模型 Qwythos-9B-Claude-Mythos）
# 容器由 /mnt/e/ai-platform/sisyphus/ 独立管理
translation:
  enabled: false
  api_base: "http://localhost:8080/v1"
  api_key: "not-needed"
  model: "sisyphus"
  target_language: "简体中文"
  timeout: 300
  batch_size: 3
  max_tokens: 4096          # 关闭推理后仅需译文 token（原 16384）
  temperature: 0.3
  repeat_penalty: 1.1
  disable_thinking: true    # 关闭推理思考（提速 60x+）
```

- `api_key: "not-needed"` 时自动识别为本地模式，传递 `repeat_penalty` + `chat_template_kwargs`
- `disable_thinking: true` 通过 `extra_body` 传递 `chat_template_kwargs: {"enable_thinking": False}`

### 5.6 输出与路径配置

```yaml
output:
  page_separator: true
  single_file: true

paths:
  input_dir: "input"
  output_dir: "output"
  log_dir: "logs"
```

---

## 六、Docker 服务编排

本项目 `docker-compose.yml` **仅管理 OCR 服务**。翻译服务由 `/mnt/e/ai-platform/sisyphus/` 独立管理。

| 服务 | 容器名 | 端口 | 框架 | 管理位置 |
|------|--------|------|------|----------|
| OCR | ocr | 8000 | vLLM | `/mnt/e/OCR/` |
| 翻译 | sisyphus | 8080 | llama.cpp | `/mnt/e/ai-platform/sisyphus/` |

### 6.1 OCR 服务配置

```yaml
image: vllm/vllm-openai:unlimited-ocr
volumes: /mnt/g/models/OCR/Unlimited-OCR-NVFP4:/model:ro
healthcheck: curl -sf http://localhost:8000/health (120s start_period)
command:
  --served-model-name unlimited-ocr
  --trust-remote-code
  --logits_processors "vllm...unlimited_ocr:NGramPerReqLogitsProcessor"  # 防长文档循环输出
  --no-enable-prefix-caching      # 多模态不适用
  --mm-processor-cache-gb 0       # 节省显存
  --max-model-len 32768
```

### 6.2 翻译服务（外部）

```yaml
# 由 ai-platform 管理，本项目不编排
image: ghcr.io/ggml-org/llama.cpp:server-cuda13
model: Qwythos-9B-Claude-Mythos-5-1M-MTP-Q8_0.gguf
端口: 8080
特性: flash-attn, q4_0 KV cache, draft-mtp 推测解码
```

```bash
# OCR 服务
cd /mnt/e/OCR && docker compose up -d
cd /mnt/e/OCR && docker compose stop

# 翻译服务（独立项目）
cd /mnt/e/ai-platform/sisyphus && docker compose up -d
```

两服务通过宿主机端口通信，无 Docker 网络依赖。显存不足以同时运行，需分时执行。

---

## 七、串行流水线

OCR（~15GB 显存）与翻译（~10GB 显存）无法同时运行，必须分时执行。

```
阶段 1: OCR
  启动 OCR 容器 → 执行 OCR（并发）→ 保存 .ocr.json → 停止容器

阶段 2: 翻译
  翻译容器已运行 → 读取 .ocr.json → 翻译 → 输出 .md / .txt
```

自动化：`run_pipeline.sh input/` 全自动调度（含容器启停 + 健康检查）。

安全保障：OCR 前自动检查并停止翻译容器，停止后等待 3-5 秒确保显存释放，轮询 `/v1/models` 确认就绪（超时 120 秒）。

---

## 八、并发 OCR

vLLM continuous batching：多请求同时到达时 GPU 自动批处理，吞吐线性增长。

实现：**预渲染 + 并发 API 分离**。串行渲染（CPU）→ 并发 API 调用（I/O），确保请求同时到达 vLLM。

| 指标 | 串行 | 并发(3) |
|------|------|---------|
| 10 页总耗时 | 105.8s | **44.8s** (-58%) |
| vLLM 吞吐 | 200 tok/s | **444 tok/s** (+122%) |
| KV cache 占用 | 1.1% | 3.2%（安全） |

---

## 九、图片页跳过

分层决策跳过广告/照片/插图页，节省 ~40% GPU 时间。内容页（文字 ≥ 1500）强保护不跳过。

OCR 后置检查：有效文本元素 < 3 个 → 标记低价值页，替换为 `[图片页 - 已跳过]` 占位。

---

## 十、OCR 质量过滤与去重

### 10.1 多层过滤体系

| 层级 | 方法 | 检测目标 |
|------|------|----------|
| **n-gram 唯一比** | 15-gram 唯一比 < 0.5 | 长文本循环重复 |
| **滑动窗口截断** | 500 字符窗口，局部唯一比 < 0.7 处截断 | 文本后半段退化 |
| **幻觉检测** | 前缀匹配已知 OCR 系统提示词 | 模型泄漏 |
| **元素级去重** | Jaccard 3-gram 相似度 ≥ 0.85 | 页内重复元素 |
| **翻译时清理** | Prompt 指令 LLM 静默去重 | 跨语义重复 |

### 10.2 质量过滤参数演进

| 参数 | v3.1 | v3.2 | 原因 |
|------|------|------|------|
| `repeat_min_length` | 3000 | **500** | 捕获短文本重复（如 775 字符的重复段落） |
| `dedup_similarity_threshold` | — | **0.85** | 新增：元素级 Jaccard 去重 |
| `sentence_repeat_threshold` | — | **3** | 新增：同一句子 N 次触发截断 |

---

## 十一、翻译优化

### 11.1 推理模型适配

Qwythos-9B-Claude-Mythos 是推理模型，响应包含 `reasoning_content`（思考过程）+ `content`（译文）。

`_call_api()` 处理逻辑：
- 提取 `content`（译文）+ `reasoning_content`（日志记录）
- `content` 为空 → 推理消耗所有 token → `max_tokens` 加倍重试（上限 32768）
- `disable_thinking: true` → 通过 `chat_template_kwargs` 关闭推理，token 消耗降至 ~15

### 11.2 批量解析优化

- Prompt `{count}` 变量 + 编号独占一行
- 解析器支持 `[N]`、`N.`、`**N.**`、`#N` 四种格式
- `batch_size: 5` → `3`（避免输出 token 耗尽截断）
- 解析成功率：60% → **100%**

### 11.3 OCR 重复主动清理

翻译 Prompt 新增指令，让 LLM 在翻译过程中自动清理 OCR 产生的重复：

- **规则 7**：重复句子/段落 → 静默去除，只翻译一次
- **规则 8**：高度相似内容 → 合并为连贯译文
- **防元注释**：严格禁止添加 "已合并"/"已去重" 等说明文字

实测效果：775 字符含重复的原文 → 164 字符干净译文（-79%），重复句子 "The process is also a step..." 出现 3 次被合并为 1 次。

### 11.4 三级容错

```
批量翻译 → 解析 → 未匹配逐条补译
     ↓ 失败
逐条翻译（_fallback_individual）
     ↓ 失败
保留原文 + warning
```

---

## 十二、运行命令

### 12.1 服务管理

```bash
# OCR 服务
cd /mnt/e/OCR && docker compose up -d
curl -sf http://localhost:8000/health

# 翻译服务（外部项目）
cd /mnt/e/ai-platform/sisyphus && docker compose up -d
curl -sf http://localhost:8080/health
```

### 12.2 主流水线

```bash
# 完整流程
python -m src.main input/book.pdf                 # 单个 PDF
python -m src.main input/ --translate             # 批量 + 翻译

# 分步模式（推荐 80+ 页大文件）
python -m src.main input/ --step ocr              # 第一步：OCR → .ocr.json
python -m src.main output/ --step translate       # 第二步：翻译 → .md
python -m src.main output/ --step translate --translate-only  # → .txt
```

### 12.3 自动化

```bash
./run_pipeline.sh input/                # 全自动（OCR + 翻译）
./run_pipeline.sh input/ --text         # 翻译纯文本输出
./run_pipeline.sh input/ --skip-ocr     # 跳过 OCR 直接翻译
```

---

## 十三、输出规范

### 13.1 文件命名

| 文件 | 说明 |
|------|------|
| `{name}.ocr.json` | OCR 中间结果（断点续传） |
| `{name}.translate.json` | 翻译 checkpoint（完成后删除） |
| `{name}.interim.md` | 每 10 页的中间 Markdown |
| `{name}.md` | 最终 Markdown |
| `{name}_translated.txt` | 翻译纯文本 |

### 13.2 JSON 格式

```json
{
  "source": "/path/to/input.pdf",
  "total_pages": 84,
  "pages": [
    { "page": 1, "skipped": false, "reason": "",
      "elements": [
        {"type": "title", "bbox": [37, 64, 464, 132], "text": "...", "page": 1}
      ]}
  ]
}
```

---

## 十四、技术栈

| 模块 | 方案 |
|------|------|
| PDF 解析 | PyMuPDF (fitz) |
| OCR 模型 | Unlimited-OCR (NVFP4, ~2.93GB) |
| OCR 推理 | vLLM (Docker, OpenAI Compatible API) |
| 翻译模型 | Qwythos-9B-Claude-Mythos (Q8_0 GGUF, 推理模型) |
| 翻译推理 | llama.cpp (Docker, CUDA, 支持 disable_thinking) |
| 并发 | ThreadPoolExecutor + vLLM continuous batching |
| 后处理 | Python + Regex + Jaccard 去重 |
| 配置 | PyYAML + python-dotenv |
| API 客户端 | openai (Python SDK) |

依赖：`PyMuPDF>=1.24.0`, `Pillow>=10.0.0`, `openai>=1.0.0`, `PyYAML>=6.0`, `tqdm>=4.60.0`, `python-dotenv>=1.0.0`

---

## 十五、最佳实践

- **DPI 保持 300**：ViT 内部下采样，降低 DPI 收益极小但有质量风险
- **JPEG quality=95**：元素数差异 <7%，速度优先
- **skip_special_tokens: false**：必须，否则 `<|det|>` 标记丢失
- **NGramPerReqLogitsProcessor**：必须，否则长文档循环输出
- **80+ 页用分步模式**：OCR 可中断恢复，两阶段独立不受显存限制
- **OCR 与翻译分时运行**：`run_pipeline.sh` 自动管理容器启停

---

## 十六、项目特点

- 全流程本地离线，不依赖云端
- 内存模式无磁盘中间文件
- 分批 OCR（每 10 页保存 + 释放）+ 并发加速（提速 58%）
- 图片页智能跳过（节省 ~40% GPU）
- 多层质量过滤（n-gram + 幻觉 + 元素去重 + 句级截断 + LLM 翻译时清理）
- 推理模型适配（reasoning_content 解析 + disable_thinking 提速 60x）
- 翻译三级容错 + 断点续传（100% 成功率）
- OCR 重复主动清理（Prompt 指令 + 后处理去重双管齐下）
- Docker 服务分离（OCR 本项目管理，翻译由 ai-platform 独立管理）

---

## 十七、扩展方向

- OCR Provider 抽象（统一接口，支持 PaddleOCR / MinerU 等替换）
- 多 GPU 支持（OCR + 翻译并行）
- 表格识别增强（专用表格模型）
- 输出格式扩展（HTML / DOCX / EPUB）
- Web UI + 语言自动检测
