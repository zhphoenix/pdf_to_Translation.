"""
PDF 转图片模块
使用 PyMuPDF 将 PDF 每页渲染为 PNG 图片
"""

from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF

from . import config
from .utils import get_logger, ensure_dir

logger = get_logger()


def pdf_to_images(
    pdf_path: Path,
    output_dir: Optional[Path] = None,
    dpi: Optional[int] = None,
    image_format: Optional[str] = None
) -> List[Path]:
    """
    将 PDF 文件的每一页转换为图片
    
    Args:
        pdf_path: PDF 文件路径
        output_dir: 输出目录，默认为 output/{pdf_name}/pages/
        dpi: 渲染 DPI，默认从配置读取（300）
        image_format: 图片格式，默认从配置读取（png）
        
    Returns:
        生成的图片文件路径列表
    """
    # 从配置获取默认值
    if dpi is None:
        dpi = config.get("pdf.dpi", 300)
    if image_format is None:
        image_format = config.get("pdf.format", "png")
    
    # 设置输出目录
    if output_dir is None:
        output_base = config.get_path("paths.output_dir")
        pdf_name = pdf_path.stem
        output_dir = output_base / pdf_name / "pages"
    
    ensure_dir(output_dir)
    
    logger.info(f"开始转换 PDF: {pdf_path.name}")
    logger.info(f"DPI: {dpi}, 格式: {image_format}")
    
    # 打开 PDF
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    logger.info(f"总页数: {total_pages}")
    
    image_paths = []
    
    # 计算缩放比例（PyMuPDF 默认 72 DPI）
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    
    for page_num in range(total_pages):
        page = doc[page_num]
        
        # 渲染页面为像素图
        pix = page.get_pixmap(matrix=matrix)
        
        # 生成输出文件名（4位数字编号）
        output_filename = f"page_{page_num + 1:04d}.{image_format}"
        output_path = output_dir / output_filename
        
        # 保存图片
        pix.save(str(output_path))
        image_paths.append(output_path)
        
        if (page_num + 1) % 10 == 0 or page_num == total_pages - 1:
            logger.info(f"已转换: {page_num + 1}/{total_pages} 页")
    
    doc.close()
    
    logger.info(f"PDF 转换完成，共生成 {len(image_paths)} 张图片")
    return image_paths


def get_page_count(pdf_path: Path) -> int:
    """
    获取 PDF 页数
    
    Args:
        pdf_path: PDF 文件路径
        
    Returns:
        页数
    """
    doc = fitz.open(str(pdf_path))
    count = len(doc)
    doc.close()
    return count


def extract_page_image(
    pdf_path: Path,
    page_num: int,
    dpi: Optional[int] = None
) -> bytes:
    """
    提取单页为图片字节（不保存到文件）
    
    Args:
        pdf_path: PDF 文件路径
        page_num: 页码（从 0 开始）
        dpi: 渲染 DPI
        
    Returns:
        PNG 图片字节数据
    """
    if dpi is None:
        dpi = config.get("pdf.dpi", 300)
    
    doc = fitz.open(str(pdf_path))
    
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        raise ValueError(f"页码超出范围: {page_num}")
    
    page = doc[page_num]
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix)
    
    image_bytes = pix.tobytes("png")
    doc.close()
    
    return image_bytes
