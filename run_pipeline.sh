#!/bin/bash
# ============================================================
# Unlimited-OCR 两阶段流水线（解决 16GB 显存不足问题）
#
# 策略：OCR 和翻译模型串行运行，避免同时占用 GPU 显存
#   阶段 1: 启动 vLLM OCR → 处理全部 PDF → 停止 OCR 容器
#   阶段 2: 启动 llama.cpp 翻译 → 翻译全部 JSON → 完成
#
# 用法:
#   ./run_pipeline.sh input/              # 处理 input 目录下所有 PDF
#   ./run_pipeline.sh input/book.pdf      # 处理单个 PDF
#   ./run_pipeline.sh input/ --text       # 翻译输出纯文本（默认 Markdown）
#   ./run_pipeline.sh input/ --skip-ocr   # 跳过 OCR（已有 JSON 时直接翻译）
# ============================================================

set -e

# ─── 配置 ───
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT="${1:-input/}"
OUTPUT_DIR="${PROJECT_DIR}/output"
OCR_CONTAINER="unlimited-ocr"
# 翻译模型 docker 参数（已迁移至 docker-compose.yml sisyphus 服务）
TRANS_CONTAINER="sisyphus"

# 输出格式
OUTPUT_FORMAT="markdown"
SKIP_OCR=false

# 解析额外参数
shift 2>/dev/null || true
for arg in "$@"; do
    case "$arg" in
        --text)      OUTPUT_FORMAT="text" ;;
        --skip-ocr)  SKIP_OCR=true ;;
        --help|-h)
            echo "用法: $0 <input_path> [--text] [--skip-ocr]"
            echo "  --text       翻译输出纯文本（默认 Markdown）"
            echo "  --skip-ocr   跳过 OCR 阶段（已有 .ocr.json 时直接翻译）"
            exit 0
            ;;
    esac
done

# ─── 工具函数 ───
log() { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')]\033[0m $1"; }
err() { echo -e "\n\033[1;31m[ERROR]\033[0m $1" >&2; }

wait_for_service() {
    local url="$1"
    local name="$2"
    local max_wait=120
    local waited=0
    log "等待 ${name} 就绪 (${url})..."
    while ! curl -s "${url}" > /dev/null 2>&1; do
        sleep 2
        waited=$((waited + 2))
        if [ $waited -ge $max_wait ]; then
            err "${name} 启动超时 (${max_wait}s)"
            return 1
        fi
    done
    log "${name} 已就绪 (等待 ${waited}s)"
}

stop_container() {
    local name="$1"
    if docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
        log "停止容器: ${name}"
        docker stop "${name}" > /dev/null 2>&1
        # 等待 GPU 显存释放
        sleep 3
    fi
}

# ─── 阶段 1: OCR ───
log "════════════════════════════════════════════════════════"
log "阶段 1/2: OCR 识别"
log "════════════════════════════════════════════════════════"

if [ "$SKIP_OCR" = true ]; then
    log "跳过 OCR（--skip-ocr）"
else
    # 确保翻译容器已停止（释放显存）
    stop_container "$TRANS_CONTAINER"

    # 启动 OCR 容器（如果未运行）
    if ! docker ps --format '{{.Names}}' | grep -q "^${OCR_CONTAINER}$"; then
        log "启动 OCR 容器: ${OCR_CONTAINER}"
        cd "$PROJECT_DIR"
        docker compose --profile ocr up -d unlimited-ocr
    fi

    # 等待 OCR 服务就绪
    wait_for_service "http://localhost:8000/v1/models" "vLLM OCR"

    # 运行 OCR 步骤
    log "执行 OCR: ${INPUT} → ${OUTPUT_DIR}/"
    cd "$PROJECT_DIR"
    python -m src.main "$INPUT" --step ocr -o "$OUTPUT_DIR"

    # 停止 OCR 容器，释放显存
    log "OCR 完成，停止 OCR 容器释放显存..."
    docker compose --profile ocr stop unlimited-ocr
    sleep 5  # 等待 GPU 显存完全释放
fi

# ─── 阶段 2: 翻译 ───
log "════════════════════════════════════════════════════════"
log "阶段 2/2: 翻译"
log "════════════════════════════════════════════════════════"

# 确保 OCR 容器已停止
stop_container "$OCR_CONTAINER"

# 启动翻译容器（如果未运行）
if ! docker ps --format '{{.Names}}' | grep -q "^${TRANS_CONTAINER}$"; then
    log "启动翻译容器: ${TRANS_CONTAINER}"
    cd "$PROJECT_DIR"
    docker compose --profile translate up -d sisyphus
fi

# 等待翻译服务就绪
wait_for_service "http://localhost:8080/v1/models" "llama.cpp 翻译"

# 运行翻译步骤
TRANSLATE_ARGS="--step translate"
if [ "$OUTPUT_FORMAT" = "text" ]; then
    TRANSLATE_ARGS="$TRANSLATE_ARGS --translate-only"
fi

log "执行翻译: ${OUTPUT_DIR}/ → 输出格式: ${OUTPUT_FORMAT}"
cd "$PROJECT_DIR"
python -m src.main "$OUTPUT_DIR" $TRANSLATE_ARGS -o "$OUTPUT_DIR"

# ─── 完成 ───
log "════════════════════════════════════════════════════════"
log "全部完成！输出目录: ${OUTPUT_DIR}/"
log "════════════════════════════════════════════════════════"

# 显示输出文件
echo ""
ls -lh "${OUTPUT_DIR}/"*.md "${OUTPUT_DIR}/"*.txt 2>/dev/null || true
