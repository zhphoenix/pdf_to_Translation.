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

## 配置

编辑 `config.yaml`：

```yaml
ocr:
  api_base: "http://localhost:8000/v1"
  model: "unlimited-ocr"
  timeout: 300
  skip_special_tokens: false
  ngram_size: 35
  ngram_window_single: 128
  ngram_window_multi: 1024

pdf:
  dpi: 300

translation:
  enabled: false    # 设为 true 启用翻译
```

## 目录结构

```
├── input/              # PDF 输入目录
├── output/             # Markdown 输出目录
├── logs/               # 日志目录
├── models/             # 模型目录（占位）
├── src/                # 源代码
│   ├── main.py         # 主流水线
│   ├── ocr.py          # OCR 引擎（vLLM API）
│   ├── pdf2image.py    # PDF 转图片
│   ├── postprocess.py  # 结构化后处理
│   ├── markdown.py     # Markdown 生成
│   ├── translate.py    # 翻译模块（可选）
│   ├── config.py       # 配置加载
│   └── utils.py        # 工具函数
├── config.yaml         # 配置文件
├── docker-compose.yml  # vLLM 服务编排
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
| 后处理 | Python + Regex（结构化元素过滤/合并） |
| 配置 | YAML |
# Unlimited-OCR 扫描版 PDF 转 Markdown

将扫描版 PDF、杂志、书籍、图片型 PDF 高质量转换成 Markdown 文本。

## 特点

- 支持扫描版 PDF、杂志、书籍、图片 PDF
- 支持 PNG、JPG、TIFF 等图片格式
- 输出高质量 Markdown
- 可本地离线运行
- 支持批量处理
- 不依赖数据库、RAG、Agent

## 架构

```
PDF → PyMuPDF（转图片）→ Unlimited-OCR（OCR）→ Markdown Cleaner → Final Markdown
```

## 安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 OCR 服务

```bash
docker-compose up -d
```

服务将在 `http://localhost:8000` 启动。

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
python -m src.main input/book.pdf --no-llm         # 跳过 LLM 后处理
python -m src.main input/book.pdf --keep-images    # 保留中间图片
python -m src.main input/book.pdf -v               # 详细输出
```

## 配置

编辑 `config.yaml`：

```yaml
ocr:
  api_base: "http://localhost:8000/v1"
  model: "unlimited-ocr"

pdf:
  dpi: 300

llm_cleanup:
  enabled: false  # 设为 true 启用 LLM 后处理
```

## 目录结构

```
├── input/          # PDF 输入目录
├── output/         # Markdown 输出目录
├── logs/           # 日志目录
├── src/            # 源代码
├── config.yaml     # 配置文件
└── docker-compose.yml
```

## 模型

需要下载 Unlimited-OCR GGUF 模型文件：

- 模型路径: `/mnt/g/models/OCR/Unlimited-OCR-BF16.gguf`
- mmproj 路径: `/mnt/g/models/OCR/mmproj.gguf`

## 技术栈

| 模块 | 方案 |
|------|------|
| PDF 解析 | PyMuPDF |
| 图片处理 | Pillow |
| OCR | Unlimited-OCR |
| 推理框架 | llama.cpp (GGUF) |
| API | OpenAI Compatible API |
| 后处理 | Python + Regex + LLM |
| 配置 | YAML |
