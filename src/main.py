#!/usr/bin/env python3
"""
Unlimited-OCR 扫描版 PDF 转 Markdown
主流水线入口

流程: PDF → 图片 → OCR(结构化) → 后处理 → 翻译(可选) → Markdown

用法:
    python -m src.main input/book.pdf
    python -m src.main input/ --translate
    python -m src.main input/ --step ocr             # 第一步：仅 OCR，保存中间 JSON
    python -m src.main output/ --step translate      # 第二步：读取 JSON 翻译，输出 Markdown
    python -m src.main input/book.pdf --translate-only
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from . import config
from .config import PROJECT_ROOT
from .pdf2image import pdf_to_images, get_page_count, extract_page_image
from .ocr import create_ocr_engine, OCRElement
from .page_filter import prescan_pages
from .postprocess import create_post_processor
from .markdown import create_markdown_generator
from .translate import create_translator
from .utils import (
    setup_logger,
    get_logger,
    get_pdf_files,
    ensure_dir,
    create_progress_bar,
    sanitize_filename
)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Unlimited-OCR 扫描版 PDF 转 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m src.main input/book.pdf              处理单个 PDF
  python -m src.main input/                      批量处理目录
  python -m src.main input/ --translate          启用翻译
  python -m src.main input/ --step ocr           第一步: 仅 OCR
  python -m src.main output/ --step translate    第二步: 仅翻译
  python -m src.main input/ --dpi 200            指定 DPI
        """
    )

    parser.add_argument(
        "input",
        type=str,
        help="输入 PDF 文件或目录路径"
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（默认: config.yaml）"
    )

    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出目录（默认: output/）"
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        help="PDF 渲染 DPI（默认: 300）"
    )

    parser.add_argument(
        "--translate",
        action="store_true",
        default=None,
        help="启用翻译（覆盖配置）"
    )

    parser.add_argument(
        "--translate-only",
        action="store_true",
        help="仅翻译模式：OCR → 后处理 → 翻译 → 输出纯文本（跳过 Markdown 生成）"
    )

    parser.add_argument(
        "--step",
        type=str,
        choices=["ocr", "translate"],
        default=None,
        help="分步执行: ocr=仅OCR+后处理并保存JSON; translate=读取JSON翻译并输出"
    )

    parser.add_argument(
        "--no-translate",
        action="store_true",
        help="禁用翻译"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出"
    )

    return parser.parse_args()


def process_single_pdf(
    pdf_path: Path,
    output_dir: Path,
    dpi: int = None,
    use_translate: bool = False,
    translate_only: bool = False
) -> Path:
    """
    处理单个 PDF 文件（完整流程）

    流程: PDF → 图片(内存) → OCR(结构化JSON) → 后处理 → 翻译(可选) → Markdown/纯文本

    Args:
        pdf_path: PDF 文件路径
        output_dir: 输出目录
        dpi: 渲染 DPI
        use_translate: 是否翻译
        translate_only: 仅翻译模式（输出纯文本，跳过 Markdown）

    Returns:
        输出文件路径
    """
    logger = get_logger()

    pdf_name = pdf_path.stem
    logger.info("=" * 60)
    logger.info(f"开始处理: {pdf_path.name}")
    logger.info("=" * 60)

    start_time = time.time()

    # 1. 获取 PDF 页数
    logger.info("[1/5] PDF 分析...")
    total_pages = get_page_count(pdf_path)
    logger.info(f"总页数: {total_pages}")

    # 2. OCR（结构化输出，内存模式，不写中间文件）
    logger.info("[2/5] OCR 识别（结构化）...")
    ocr_engine = create_ocr_engine()

    all_pages_elements: List[List[OCRElement]] = []
    progress = create_progress_bar(total_pages, "OCR")

    for i in range(total_pages):
        try:
            image_bytes = extract_page_image(pdf_path, i, dpi=dpi)
            elements = ocr_engine.ocr_image_bytes(image_bytes, page_num=i + 1)
            all_pages_elements.append(elements)
            logger.debug(f"  第 {i+1} 页: {len(elements)} 个元素")
        except Exception as e:
            logger.error(f"OCR 失败: 第 {i+1} 页, {e}")
            all_pages_elements.append([])
        progress.update(1)

    progress.close()
    total_elements = sum(len(p) for p in all_pages_elements)
    logger.info(f"OCR 完成: {len(all_pages_elements)} 页, {total_elements} 个元素")

    # 3. 后处理（结构化）
    logger.info("[3/5] 后处理...")
    post_processor = create_post_processor()
    processed_pages = post_processor.process_document(all_pages_elements)
    remaining_elements = sum(len(p) for p in processed_pages)
    logger.info(f"后处理完成: 保留 {remaining_elements}/{total_elements} 个元素")

    # 4. 翻译（可选）
    if translate_only:
        # 仅翻译模式：OCR → 后处理 → 翻译 → 纯文本输出
        logger.info("[4/5] 翻译（纯文本输出模式）...")
        translator = create_translator(enabled=True)
        final_text = translator.translate_pages_to_text(processed_pages)

        # 保存为 .txt
        output_file = output_dir / f"{sanitize_filename(pdf_name)}_translated.txt"
        ensure_dir(output_dir)

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(final_text)

        elapsed = time.time() - start_time
        logger.info(f"翻译完成: {output_file}")
        logger.info(f"耗时: {elapsed:.1f} 秒")
        return output_file

    elif use_translate:
        logger.info("[4/5] 翻译...")
        translator = create_translator(enabled=True)
        processed_pages = translator.translate_pages(processed_pages)
        logger.info("翻译完成")
    else:
        logger.info("[4/5] 翻译已跳过")

    # 5. 生成 Markdown
    logger.info("[5/5] 生成 Markdown...")
    generator = create_markdown_generator()
    final_markdown = generator.generate_document(processed_pages)

    # 保存输出
    output_file = output_dir / f"{sanitize_filename(pdf_name)}.md"
    ensure_dir(output_dir)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_markdown)

    elapsed = time.time() - start_time
    logger.info(f"处理完成: {output_file}")
    logger.info(f"耗时: {elapsed:.1f} 秒")

    return output_file


# ─── 分步模式 ─────────────────────────────────────────────────────

# 翻译容器名称（OCR 前需检查是否占用显存）
TRANS_CONTAINER_NAME = "sisyphus"

# 跳过页占位符文本前缀
SKIP_PLACEHOLDER_PREFIX = "[图片页 - 已跳过]"


def _count_text_elements(elements: List[OCRElement]) -> int:
    """统计页面中有效文本元素数（排除页眉/页脚/页码）"""
    skip_types = {"header", "footer", "page_number"}
    return sum(
        1 for e in elements
        if e.text.strip() and e.type not in skip_types
    )


def _make_placeholder_element(page_num: int, reason: str) -> OCRElement:
    """为跳过/低价值页生成占位元素，保持页码连续"""
    if reason.startswith("low-value"):
        text = f"{SKIP_PLACEHOLDER_PREFIX}（OCR 识别内容过少）"
    else:
        text = f"{SKIP_PLACEHOLDER_PREFIX}（{reason}）"
    return OCRElement(type="text", bbox=(0, 0, 0, 0), text=text, page=page_num)


def _ensure_translation_stopped():
    """
    检查翻译模型容器是否在运行，若是则自动停止以释放显存。
    """
    logger = get_logger()
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        running_containers = result.stdout.strip().splitlines()
        if TRANS_CONTAINER_NAME in running_containers:
            logger.warning(f"检测到翻译容器 '{TRANS_CONTAINER_NAME}' 正在运行，停止以释放显存...")
            subprocess.run(
                ["docker", "stop", TRANS_CONTAINER_NAME],
                capture_output=True, timeout=30
            )
            import time as _t
            _t.sleep(3)  # 等待 GPU 显存释放
            logger.info(f"已停止 '{TRANS_CONTAINER_NAME}'，显存已释放")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug(f"Docker 检查跳过: {e}")

def _ocr_single_page(ocr_engine, pdf_path, page_idx, page_no, dpi):
    """
    单页 OCR 工作函数（渲染 + API，串行回退用）

    Returns:
        (page_idx, elements) 元组
    """
    image_bytes = extract_page_image(pdf_path, page_idx, dpi=dpi)
    elements = ocr_engine.ocr_image_bytes(image_bytes, page_num=page_no)
    return page_idx, elements


def _ocr_api_only(ocr_engine, image_bytes, page_no):
    """
    仅 API 调用（预渲染后并发用，消除渲染延迟对并发的阻塞）

    Returns:
        elements 列表
    """
    return ocr_engine.ocr_image_bytes(image_bytes, page_num=page_no)


def step_ocr(pdf_files: List[Path], output_dir: Path, dpi: int = None):
    """
    第一步：仅 OCR + 后处理，保存中间 JSON

    支持并发：通过 ocr.concurrency 配置客户端并发数，
    利用 vLLM continuous batching 提升 GPU 利用率。

    输出: output_dir/<pdf_name>.ocr.json
    """
    logger = get_logger()
    logger.info("=" * 60)
    logger.info("[步骤 1/2] OCR 识别 + 后处理")
    logger.info("=" * 60)

    # 检查翻译模型是否占用显存
    _ensure_translation_stopped()

    ocr_engine = create_ocr_engine()
    post_processor = create_post_processor()
    ensure_dir(output_dir)

    # 页面预检阈值
    skip_enabled = config.get("page_filter.skip_image_pages", True)
    min_ocr_elements = config.get("page_filter.min_ocr_elements", 3)
    concurrency = max(1, config.get("ocr.concurrency", 2))

    if concurrency > 1:
        logger.info(f"并发模式: {concurrency} 路请求")

    success = 0
    for pdf_path in pdf_files:
        pdf_name = pdf_path.stem
        json_file = output_dir / f"{sanitize_filename(pdf_name)}.ocr.json"

        # 跳过已存在的 JSON（支持断点续传）
        if json_file.exists():
            logger.info(f"跳过已存在: {json_file.name}")
            success += 1
            continue

        logger.info(f"OCR 处理: {pdf_path.name}")
        start_time = time.time()

        try:
            total_pages = get_page_count(pdf_path)

            # 页面预检：识别图片页（OCR 前，节省 GPU）
            page_analysis = prescan_pages(pdf_path) if skip_enabled else {}

            # ─── 第一阶段：分类页面 ───
            # pages_data[i] 预填充跳过页，OCR 页留 None 待填
            pages_data: List[Optional[dict]] = [None] * total_pages
            ocr_tasks = []  # (page_idx, page_no) 需要 OCR 的页面
            skipped_count = 0

            for i in range(total_pages):
                page_no = i + 1
                analysis = page_analysis.get(i)
                if analysis is not None and analysis.is_image_only:
                    logger.info(
                        f"跳过第 {page_no} 页（图片页，{analysis.reason}，"
                        f"文字字符数: {analysis.text_chars}）"
                    )
                    pages_data[i] = {
                        "page": page_no,
                        "skipped": True,
                        "reason": f"image-only page ({analysis.reason})",
                        "elements": [_make_placeholder_element(page_no, analysis.reason).to_dict()],
                    }
                    skipped_count += 1
                else:
                    ocr_tasks.append((i, page_no))

            # ─── 第二阶段：预渲染 + 并发 OCR API ───
            low_value_count = 0
            ocr_results = {}  # page_idx -> elements
            progress = create_progress_bar(total_pages, f"OCR {pdf_name[:20]}")
            # 先更新已跳过页的进度
            progress.update(skipped_count)

            if concurrency <= 1 or len(ocr_tasks) <= 1:
                # 串行回退
                for page_idx, page_no in ocr_tasks:
                    try:
                        _, elements = _ocr_single_page(ocr_engine, pdf_path, page_idx, page_no, dpi)
                    except Exception as e:
                        logger.error(f"  OCR 失败: 第 {page_no} 页, {e}")
                        elements = []
                    ocr_results[page_idx] = elements
                    progress.update(1)
            else:
                # 预渲染所有页面（CPU 密集，串行执行，JPEG 编码很快）
                rendered_images = {}  # page_idx -> image_bytes
                for page_idx, page_no in ocr_tasks:
                    try:
                        rendered_images[page_idx] = extract_page_image(pdf_path, page_idx, dpi=dpi)
                    except Exception as e:
                        logger.error(f"  渲染失败: 第 {page_no} 页, {e}")
                        rendered_images[page_idx] = None

                # 并发 API 调用（I/O 密集，充分利用 vLLM continuous batching）
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    future_map = {}
                    for page_idx, page_no in ocr_tasks:
                        img = rendered_images.get(page_idx)
                        if img is None:
                            ocr_results[page_idx] = []
                            progress.update(1)
                            continue
                        future = executor.submit(
                            _ocr_api_only, ocr_engine, img, page_no
                        )
                        future_map[future] = (page_idx, page_no)

                    for future in as_completed(future_map):
                        page_idx, page_no = future_map[future]
                        try:
                            elements = future.result()
                        except Exception as e:
                            logger.error(f"  OCR 失败: 第 {page_no} 页, {e}")
                            elements = []
                        ocr_results[page_idx] = elements
                        progress.update(1)

                # 释放渲染图片内存
                rendered_images.clear()

            progress.close()

            # ─── 第三阶段：后处理 + 组装结果（按页序）───
            for page_idx, _ in ocr_tasks:
                page_no = page_idx + 1
                elements = ocr_results.get(page_idx, [])

                # 后处理（单页）
                elements = post_processor.process_page(elements)

                # 后置检查：有效文本元素过少 → 标记低价值页
                page_skipped = False
                page_reason = ""
                if skip_enabled and _count_text_elements(elements) < min_ocr_elements:
                    page_skipped = True
                    page_reason = f"low-value page ({_count_text_elements(elements)} text elements)"
                    logger.info(f"第 {page_no} 页 OCR 内容过少，标记为低价值页")
                    elements = [_make_placeholder_element(page_no, "low-value")]
                    low_value_count += 1

                pages_data[page_idx] = {
                    "page": page_no,
                    "skipped": page_skipped,
                    "reason": page_reason,
                    "elements": [e.to_dict() for e in elements],
                }

            # 保存页级 JSON
            data = {
                "source": str(pdf_path),
                "total_pages": total_pages,
                "pages": pages_data,
            }
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            elapsed = time.time() - start_time
            total_elem = sum(len(p["elements"]) for p in pages_data)
            logger.info(
                f"  完成: {total_elem} 个元素, 跳过 {skipped_count} 图片页"
                f" + {low_value_count} 低价值页, 耗时 {elapsed:.1f}s → {json_file.name}"
            )
            success += 1

        except Exception as e:
            logger.error(f"  失败: {pdf_path.name}, {e}")

    logger.info(f"OCR 步骤完成: {success}/{len(pdf_files)} 个文件")
    return success


def step_translate(input_dir: Path, output_dir: Path, output_format: str = "markdown"):
    """
    第二步：读取中间 JSON，翻译并输出

    Args:
        input_dir: 包含 .ocr.json 的目录
        output_dir: 输出目录
        output_format: 输出格式 (markdown / text)
    """
    logger = get_logger()
    logger.info("=" * 60)
    logger.info("[步骤 2/2] 翻译 + 输出")
    logger.info("=" * 60)

    # 查找所有 .ocr.json 文件
    json_files = sorted(input_dir.glob("*.ocr.json"))
    if not json_files:
        logger.error(f"未找到 .ocr.json 文件: {input_dir}")
        return 0

    logger.info(f"找到 {len(json_files)} 个 OCR 结果文件")

    translator = create_translator(enabled=True)
    ensure_dir(output_dir)

    success = 0
    for json_file in json_files:
        base_name = json_file.stem.replace(".ocr", "")
        logger.info(f"翻译: {json_file.name}")
        start_time = time.time()

        try:
            # 读取 JSON
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 反序列化元素（兼容新页级格式与旧纯列表格式）
            raw_pages = data["pages"]
            pages: List[List[OCRElement]] = []
            for p in raw_pages:
                if isinstance(p, dict):
                    # 新格式：{"page", "skipped", "reason", "elements": [...]}
                    elems = [OCRElement.from_dict(d) for d in p.get("elements", [])]
                else:
                    # 旧格式：直接是元素 dict 列表
                    elems = [OCRElement.from_dict(d) for d in p]
                pages.append(elems)

            if output_format == "text":
                # 纯文本输出
                final_text = translator.translate_pages_to_text(pages)
                output_file = output_dir / f"{sanitize_filename(base_name)}_translated.txt"
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(final_text)
            else:
                # Markdown 输出
                translated_pages = translator.translate_pages(pages)
                generator = create_markdown_generator()
                final_markdown = generator.generate_document(translated_pages)
                output_file = output_dir / f"{sanitize_filename(base_name)}.md"
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(final_markdown)

            elapsed = time.time() - start_time
            logger.info(f"  完成: 耗时 {elapsed:.1f}s → {output_file.name}")
            success += 1

        except Exception as e:
            logger.error(f"  翻译失败: {json_file.name}, {e}")

    logger.info(f"翻译步骤完成: {success}/{len(json_files)} 个文件")
    return success


def main():
    """主函数"""
    args = parse_args()

    # 加载配置
    if args.config:
        config.load_config(args.config)
    else:
        config.load_config()

    # 设置日志
    import logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logger = setup_logger(level=log_level)

    # 更新配置
    if args.dpi:
        config.update_config({"pdf.dpi": args.dpi})

    # 确定输出目录
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = config.get_path("paths.output_dir")

    ensure_dir(output_dir)

    # ─── 分步模式 ───
    if args.step == "ocr":
        # 第一步：仅 OCR，输入必须是 PDF
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = PROJECT_ROOT / input_path
        try:
            pdf_files = get_pdf_files(input_path)
        except (ValueError, FileNotFoundError) as e:
            logger.error(str(e))
            sys.exit(1)
        logger.info(f"找到 {len(pdf_files)} 个 PDF 文件")
        step_ocr(pdf_files, output_dir, dpi=args.dpi)
        return

    elif args.step == "translate":
        # 第二步：仅翻译，输入是包含 .ocr.json 的目录
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = PROJECT_ROOT / input_path
        if not input_path.is_dir():
            logger.error(f"翻译步骤需要目录作为输入: {input_path}")
            sys.exit(1)
        # 输出格式：--translate-only 输出纯文本，否则 Markdown
        fmt = "text" if args.translate_only else "markdown"
        step_translate(input_path, output_dir, output_format=fmt)
        return

    # ─── 完整流程模式 ───
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path

    try:
        pdf_files = get_pdf_files(input_path)
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info(f"找到 {len(pdf_files)} 个 PDF 文件")

    # 是否翻译
    translate_only = args.translate_only
    if args.no_translate:
        use_translate = False
    elif args.translate or translate_only:
        use_translate = True
    else:
        use_translate = config.get("translation.enabled", False)

    # 处理每个 PDF
    output_files = []

    for pdf_path in pdf_files:
        try:
            output_file = process_single_pdf(
                pdf_path=pdf_path,
                output_dir=output_dir,
                dpi=args.dpi,
                use_translate=use_translate,
                translate_only=translate_only
            )
            output_files.append(output_file)
        except Exception as e:
            logger.error(f"处理失败: {pdf_path.name}, {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()

    # 汇总
    logger.info("=" * 60)
    logger.info(f"全部完成！成功处理 {len(output_files)}/{len(pdf_files)} 个文件")
    for f in output_files:
        logger.info(f"  - {f}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
