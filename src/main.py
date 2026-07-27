#!/usr/bin/env python3
"""
Unlimited-OCR 扫描版 PDF 转 Markdown
主流水线入口

流程: PDF → 图片 → OCR(结构化) → 后处理 → 翻译(可选) → Markdown

用法:
    python -m src.main input/book.pdf
    python -m src.main input/
    python -m src.main input/book.pdf --translate
    python -m src.main input/book.pdf --no-translate
"""

import argparse
import sys
import time
from pathlib import Path
from typing import List

from . import config
from .config import PROJECT_ROOT
from .pdf2image import pdf_to_images, get_page_count, extract_page_image
from .ocr import create_ocr_engine, OCRElement
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
    use_translate: bool = False
) -> Path:
    """
    处理单个 PDF 文件

    流程: PDF → 图片(内存) → OCR(结构化JSON) → 后处理 → 翻译(可选) → Markdown

    Args:
        pdf_path: PDF 文件路径
        output_dir: 输出目录
        dpi: 渲染 DPI
        use_translate: 是否翻译

    Returns:
        输出的 Markdown 文件路径
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
    if use_translate:
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

    # 获取 PDF 文件列表
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
    if args.no_translate:
        use_translate = False
    elif args.translate:
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
                use_translate=use_translate
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
