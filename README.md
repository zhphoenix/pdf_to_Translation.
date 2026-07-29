# Unlimited-OCR 扫描版 PDF 转 Markdown

将扫描版 PDF、杂志、书籍、图片型 PDF 高质量转换成 Markdown 文本。

## 特点

- 支持扫描版 PDF、杂志、书籍、图片 PDF
- 输出高质量结构化 Markdown（标题、段落、表格、列表）
- OCR 输出结构化 JSON（type + bbox + text），后处理恢复文档结构
- 可本地离线运行
- 支持批量处理
- 不依赖数据库、RAG、Agent

## 架构

```
PDF → PyMuPDF（内存转图片）→ vLLM Unlimited-OCR（结构化 OCR）→ 后处理 → Markdown
```

## 安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 下载模型

```bash
hf download sahilchachra/Unlimited-OCR-NVFP4 \
  --local-dir /mnt/g/models/OCR/Unlimited-OCR-NVFP4
```

### 3. 启动 OCR 服务

```bash
docker-compose up -d
```

验证服务：

```bash
curl http://localhost:8000/health       # → 200
curl http://localhost:8000/v1/models    # → {"data":[{"id":"unlimited-ocr",...}]}
```

## 使用

### 处理单个 PDF

```bash
python -m src.main input/book.pdf
```

### 批量处理

```bash
python -m src.main input/
```

### 命令行选项

```bash
python -m src.main input/book.pdf --dpi 200        # 指定 DPI
python -m src.main input/book.pdf --translate      # 启用翻译（默认关闭）
python -m src.main input/book.pdf --no-translate   # 禁用翻译
python -m src.main input/book.pdf -v               # 详细输出
```

## 运行命令大全

### 一、服务管理（Docker Compose）

OCR 与翻译服务使用 profiles 隔离，**不可同时启动**（16GB 显存不足）。

```bash
# ── OCR 服务（vLLM，端口 8000）──
docker compose --profile ocr up -d unlimited-ocr      # 启动 OCR 服务
docker compose --profile ocr stop unlimited-ocr       # 停止 OCR 服务
docker logs -f unlimited-ocr                          # 查看 OCR 日志

# ── 翻译服务（llama.cpp，端口 8080）──
docker compose --profile translate up -d sisyphus     # 启动翻译服务
docker compose --profile translate stop sisyphus      # 停止翻译服务
docker logs -f sisyphus                               # 查看翻译日志

# ── 通用 ──
docker ps                                             # 查看运行中的容器
docker stop unlimited-ocr sisyphus                    # 停止全部
docker compose down                                   # 停止并移除容器
```

### 二、服务健康检查

```bash
# OCR 服务
curl http://localhost:8000/health                     # → 200 表示就绪
curl http://localhost:8000/v1/models                  # → 模型列表

# 翻译服务
curl http://localhost:8080/health                     # → 200 表示就绪
curl http://localhost:8080/v1/models                  # → 模型列表

# GPU 状态
nvidia-smi                                            # 查看显存占用
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
```

### 三、主流水线（python -m src.main）

```bash
# ── 完整流程（OCR → 后处理 → Markdown）──
python -m src.main input/book.pdf                     # 单个 PDF
python -m src.main input/                             # 批量处理目录
python -m src.main input/book.pdf -o output/          # 指定输出目录
python -m src.main input/book.pdf --dpi 200           # 指定渲染 DPI
python -m src.main input/book.pdf --config my.yaml    # 指定配置文件
python -m src.main input/book.pdf -v                  # 详细日志

# ── 带翻译的完整流程 ──
python -m src.main input/ --translate                 # 启用翻译输出 Markdown
python -m src.main input/ --no-translate              # 禁用翻译
python -m src.main input/ --translate-only            # OCR→翻译→纯文本（跳过 Markdown）

# ── 分步模式（推荐，配合显存串行策略）──
python -m src.main input/ --step ocr                  # 第一步：仅 OCR，保存 .ocr.json
python -m src.main output/ --step translate           # 第二步：读取 JSON 翻译输出 Markdown
python -m src.main output/ --step translate --translate-only   # 第二步：翻译输出纯文本
```

#### 命令行参数说明

| 参数 | 说明 |
|------|------|
| `input` | 输入 PDF 文件或目录路径（必填） |
| `--config` | 配置文件路径（默认 `config.yaml`） |
| `--output`, `-o` | 输出目录（默认 `output/`） |
| `--dpi` | PDF 渲染 DPI（默认 300） |
| `--translate` | 启用翻译（覆盖配置） |
| `--translate-only` | 仅翻译模式，输出纯文本 |
| `--step {ocr,translate}` | 分步执行 |
| `--no-translate` | 禁用翻译 |
| `--verbose`, `-v` | 详细输出 |

### 四、两阶段自动化编排（run_pipeline.sh）

自动调度容器启停：停翻译 → 启 OCR → 处理 → 停 OCR → 启翻译 → 翻译。

```bash
./run_pipeline.sh input/                  # 处理 input 目录（默认输出 Markdown）
./run_pipeline.sh input/book.pdf          # 处理单个 PDF
./run_pipeline.sh input/ --text           # 翻译输出纯文本
./run_pipeline.sh input/ --skip-ocr       # 跳过 OCR（已有 .ocr.json 时直接翻译）
./run_pipeline.sh --help                  # 查看帮助
```

### 五、典型工作流

```bash
# 场景 A：一键全自动（推荐新手）
./run_pipeline.sh input/

# 场景 B：手动分步（适合 80+ 页大文件，可中断恢复）
docker compose --profile ocr up -d unlimited-ocr      # 1. 启 OCR
python -m src.main input/ --step ocr                  # 2. 跑 OCR（生成 .ocr.json）
docker compose --profile ocr stop unlimited-ocr       # 3. 停 OCR 释放显存
docker compose --profile translate up -d sisyphus     # 4. 启翻译
python -m src.main output/ --step translate           # 5. 跑翻译
docker compose --profile translate stop sisyphus      # 6. 停翻译

# 场景 C：仅 OCR 不翻译（快速评估识别质量）
python -m src.main input/ --step ocr --no-translate
```

## 配置

编辑 `config.yaml`：

```yaml
ocr:
  api_base: "http://localhost:8000/v1"
  model: "unlimited-ocr"
  timeout: 300
  concurrency: 3              # 客户端并发数（利用 vLLM continuous batching）
  skip_special_tokens: false
  ngram_size: 35
  ngram_window_single: 128
  ngram_window_multi: 1024

pdf:
  dpi: 300
  ocr_format: "jpeg"          # OCR 传输格式（jpeg 快+小，png 无损）

page_filter:
  skip_image_pages: true      # OCR 前自动跳过图片页
  body_text_threshold: 1500   # 文字量超过此值→内容页（强保护）
  high_image_coverage: 0.85   # 图片覆盖超过此值→广告页

translation:
  enabled: false              # 设为 true 启用翻译
```

## 目录结构

```
├── input/              # PDF 输入目录
├── output/             # 输出目录（.ocr.json / .md / .txt）
├── logs/               # 日志目录
├── src/                # 源代码
│   ├── main.py         # 主流水线（分步模式 + 并发 OCR）
│   ├── ocr.py          # OCR 引擎（vLLM API）
│   ├── pdf2image.py    # PDF 转图片（JPEG/PNG）
│   ├── page_filter.py  # 图片页预检与跳过
│   ├── postprocess.py  # 结构化后处理
│   ├── markdown.py     # Markdown 生成
│   ├── translate.py    # 翻译模块（可选）
│   ├── config.py       # 配置加载
│   └── utils.py        # 工具函数
├── config.yaml         # 配置文件
├── docker-compose.yml  # 服务编排（OCR + 翻译 profiles 隔离）
├── run_pipeline.sh     # 两阶段自动化编排脚本
└── requirements.txt    # Python 依赖
```

## 模型

- 来源: [sahilchachra/Unlimited-OCR-NVFP4](https://huggingface.co/sahilchachra/Unlimited-OCR-NVFP4)
- 格式: NVFP4 量化 SafeTensors (~2.93GB)
- 本地路径: `/mnt/g/models/OCR/Unlimited-OCR-NVFP4`
- 基础模型: [baidu/Unlimited-OCR](https://github.com/baidu/Unlimited-OCR) (MIT)

## 技术栈

| 模块 | 方案 |
|------|------|
| PDF 解析 | PyMuPDF |
| OCR 模型 | Unlimited-OCR (NVFP4) |
| 推理框架 | vLLM (Docker) |
| Docker 镜像 | `vllm/vllm-openai:unlimited-ocr` |
| API | OpenAI Compatible API |
| 并发 | ThreadPoolExecutor + vLLM continuous batching |
| 后处理 | Python + Regex（结构化元素过滤/合并） |
| 配置 | YAML |

## 性能优化记录（2026-07-28）

### 修改目标

在 16GB 显存安全策略下进一步提速，解决并发退化为串行的问题。

### 修改文件列表

| 文件 | 修改内容 |
|------|----------|
| `src/main.py` | 预渲染+并发API分离，新增 `_ocr_api_only()` |
| `src/pdf2image.py` | 支持 JPEG 编码输出，`alpha=False` 减少内存 |
| `src/ocr.py` | 自动检测 JPEG/PNG magic bytes 设置 MIME |
| `src/page_filter.py` | 内容页早退（跳过不必要的 pixmap 渲染） |
| `config.yaml` | 新增 `pdf.ocr_format`，`ocr.concurrency` 提升为 3 |

### 每项修改的原因

1. **预渲染+并发API分离**：原方案中渲染和API调用在同一线程，PNG编码耗时3-5s导致并发请求无法同时到达vLLM，实际退化为串行
2. **JPEG替代PNG**：编码速度提升3-5x，体积缩小70%，减少HTTP传输时间
3. **page_filter早退**：内容页(text≥1500)无需执行昂贵的pixmap渲染+颜色分析
4. **concurrency=3**：KV cache仅用3.2%，显存无额外增长

### 性能对比

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| 10页PDF总耗时 | 105.8s | **44.8s** | **-57.7%** |
| 有效OCR页均耗时 | 17.6s/页 | **7.5s/页** | -57% |
| vLLM generation吞吐 | 200 tok/s | **444 tok/s** | +122% |
| vLLM 实际并发 | 1 req (退化) | **3 reqs** | 修复 |
| GPU KV cache | 1.1% | 3.2% | 安全 |
| GPU显存峰值 | 14979 MiB | 14989 MiB | 无变化 |

测试条件: `The_Economist_Europe_-_30_May_2026_1-10.pdf`，10页中4页广告跳过，6页实际OCR。

### 显存与稳定性风险评估

- **显存**: KV cache 已预分配，并发不增加显存。concurrency=3 仅用 3.2%，可安全提升至 4
- **JPEG 质量**: quality=95 保留细节，元素数差异 <7%（视觉模型正常波动）
- **超时**: 并发时单页分到的 GPU 时间减少，密集页可能需更长时间，当前 timeout=300s 充足
- **回滚**: 将 `ocr.concurrency` 改为 1、`pdf.ocr_format` 改为 `"png"` 即可回退

### 推荐生产配置

```yaml
ocr:
  concurrency: 3          # RTX 5060 Ti 16GB 安全值
  timeout: 300
pdf:
  dpi: 300
  ocr_format: "jpeg"     # 速度优先
page_filter:
  skip_image_pages: true  # 跳过广告页节省 ~40% GPU 时间
```

### 回滚方法

```yaml
# config.yaml 回滚为串行 + PNG
ocr:
  concurrency: 1
pdf:
  ocr_format: "png"
```
# python -m src.main input/ --step ocr && python -m src.main output/ --step translate 