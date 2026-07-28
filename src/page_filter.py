"""
页面预检模块
在 OCR 调用之前判断页面是否为"以图片为主"的页面（全页照片、广告图、插图等），
从而跳过无意义的 OCR 调用，节省 GPU 时间与显存。

判断依据（分层决策）：
1. 正文文字量：文字层字符数充足→必为内容页（强保护，避免误伤图文混排）
2. 嵌入图片面积占比 + 颜色丰富度：文字稀疏且大图覆盖→广告/图片页
3. 内嵌文字层字符数：辅助区分数字版与纯扫描版

注意：本模块只判断"整页是否为图片"，不影响页面内正常图文混排的 OCR。
"""

from dataclasses import dataclass
from typing import Dict, Optional

import fitz  # PyMuPDF

from . import config
from .utils import get_logger

logger = get_logger()


@dataclass
class PageAnalysis:
    """单页分析结果"""
    page_num: int                 # 页码（0 开始）
    is_image_only: bool           # 是否为图片页
    reason: str                   # 判定原因（用于日志与 JSON 记录）
    text_chars: int = 0           # 内嵌文字层字符数
    image_coverage: float = 0.0   # 嵌入图片面积占比
    color_count: int = 0          # 降采样后不同颜色数（颜色丰富度）
    ink_coverage: float = 0.0     # 非白像素占比（墨迹覆盖率）


def _analyze_pixmap_colors(pix, samples: int = 12000):
    """
    对像素图降采样，统计颜色丰富度与墨迹覆盖率。

    - color_count: 抽样像素中不同颜色（按 32 级量化）的数量
    - ink_coverage: 非白像素（任一通道 < 245）占比

    Args:
        pix: fitz.Pixmap（RGB/RGBA）
        samples: 抽样像素数

    Returns:
        (color_count, ink_coverage)
    """
    w, h = pix.width, pix.height
    n = pix.n
    data = pix.samples
    total = w * h

    if total == 0:
        return 0, 0.0

    # 等间隔抽样
    step = max(1, total // samples)
    colors = set()
    non_white = 0
    count = 0

    for i in range(0, total, step):
        off = i * n
        r = data[off]
        g = data[off + 1]
        b = data[off + 2]
        # 量化到 32 级（>>5），降低抗锯齿噪声
        colors.add((r >> 5, g >> 5, b >> 5))
        if r < 245 or g < 245 or b < 245:
            non_white += 1
        count += 1

    ink_coverage = non_white / count if count else 0.0
    return len(colors), ink_coverage


def analyze_page(
    doc: "fitz.Document",
    page_num: int,
    min_text_chars: int = 50,
    body_text_threshold: int = 1500,
    image_coverage_threshold: float = 0.6,
    high_image_coverage: float = 0.85,
    color_threshold: int = 100,
    ink_min: float = 0.08,
    ink_max: float = 0.75,
    render_dpi: int = 72,
) -> PageAnalysis:
    """
    分析单页是否为图片页。

    分层决策：
      1. text_chars >= body_text_threshold → 内容页（强保护）
      2. 文字稀疏 + 图片覆盖 >= high_image_coverage：
         - 颜色少 → 图形广告；颜色多 → 照片广告
      3. 文字极少(< min_text_chars) + 图片覆盖 >= image_coverage_threshold → 图片页
      4. 文字极少 + 颜色丰富 + 墨迹适中 → 照片页（纯扫描版无文字层）

    Args:
        doc: 已打开的 fitz.Document
        page_num: 页码（0 开始）
        min_text_chars: 极少文字阈值（纯扫描版图片页判定）
        body_text_threshold: 正文文字量阈值（超过则必为内容页）
        image_coverage_threshold: 图片覆盖中等阈值
        high_image_coverage: 图片覆盖高阈值（广告页判定）
        color_threshold: 颜色丰富度阈值（区分照片与图形）
        ink_min: 墨迹覆盖率下限
        ink_max: 墨迹覆盖率上限
        render_dpi: 颜色分析用的渲染 DPI

    Returns:
        PageAnalysis 结果
    """
    page = doc[page_num]
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height if page_rect else 1.0

    # 1. 内嵌文字层字符数
    text_chars = len(page.get_text("text").strip())

    # ─── 快速路径：文字量充足→直接判定内容页，跳过昂贵的图片/颜色分析 ───
    if text_chars >= body_text_threshold:
        return PageAnalysis(
            is_image_only=False, reason="content-page",
            page_num=page_num, text_chars=text_chars,
            image_coverage=0.0, color_count=0, ink_coverage=0.0,
        )

    # 2. 嵌入图片面积占比
    image_coverage = 0.0
    for img in page.get_image_info(xrefs=True):
        bbox = img.get("bbox")
        if bbox:
            iw = bbox[2] - bbox[0]
            ih = bbox[3] - bbox[1]
            image_coverage += (iw * ih) / page_area
    image_coverage = min(image_coverage, 1.0)

    # 3. 渲染图颜色分析（低 DPI）
    zoom = render_dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    color_count, ink_coverage = _analyze_pixmap_colors(pix)
    pix = None  # 释放

    base = dict(
        page_num=page_num, text_chars=text_chars,
        image_coverage=image_coverage, color_count=color_count,
        ink_coverage=ink_coverage,
    )

    # ─── 分层判定（文字量充足的已在前面早退）───
    # 2. 文字稀疏 + 大图覆盖几乎整页 → 广告/图片页
    if image_coverage >= high_image_coverage:
        if color_count < color_threshold:
            return PageAnalysis(
                is_image_only=True,
                reason=f"graphic-ad (coverage {image_coverage:.0%}, colors {color_count})",
                **base,
            )
        return PageAnalysis(
            is_image_only=True,
            reason=f"photo-ad (coverage {image_coverage:.0%}, colors {color_count})",
            **base,
        )

    # 3. 文字极少 + 中等以上图片覆盖 → 图片页（纯扫描版）
    if text_chars < min_text_chars and image_coverage >= image_coverage_threshold:
        return PageAnalysis(
            is_image_only=True,
            reason=f"image-coverage {image_coverage:.0%}",
            **base,
        )

    # 4. 文字极少 + 颜色丰富 + 墨迹适中 → 照片页（无文字层全页照片）
    if text_chars < min_text_chars and color_count >= color_threshold and ink_min <= ink_coverage <= ink_max:
        return PageAnalysis(
            is_image_only=True,
            reason=f"photo-colors {color_count} (ink {ink_coverage:.0%})",
            **base,
        )

    # 其余保留
    return PageAnalysis(is_image_only=False, reason="keep", **base)


def prescan_pages(
    pdf_path,
    dpi: Optional[int] = None,
) -> Dict[int, PageAnalysis]:
    """
    预扫描整个 PDF，返回每页的分析结果。

    Args:
        pdf_path: PDF 文件路径
        dpi: 颜色分析渲染 DPI（默认从配置读取，限制较低值以省内存）

    Returns:
        {page_num: PageAnalysis} 字典
    """
    enabled = config.get("page_filter.skip_image_pages", True)
    results: Dict[int, PageAnalysis] = {}

    doc = fitz.open(str(pdf_path))
    total = len(doc)

    if not enabled:
        # 关闭时仍返回空 dict，调用方据此跳过预检
        doc.close()
        return results

    min_text_chars = config.get("page_filter.min_text_chars", 50)
    body_text_threshold = config.get("page_filter.body_text_threshold", 1500)
    image_coverage_threshold = config.get("page_filter.image_coverage_threshold", 0.6)
    high_image_coverage = config.get("page_filter.high_image_coverage", 0.85)
    color_threshold = config.get("page_filter.color_threshold", 100)
    ink_min = config.get("page_filter.ink_coverage_min", 0.08)
    ink_max = config.get("page_filter.ink_coverage_max", 0.75)
    render_dpi = dpi or config.get("page_filter.render_dpi", 72)

    for i in range(total):
        results[i] = analyze_page(
            doc, i,
            min_text_chars=min_text_chars,
            body_text_threshold=body_text_threshold,
            image_coverage_threshold=image_coverage_threshold,
            high_image_coverage=high_image_coverage,
            color_threshold=color_threshold,
            ink_min=ink_min,
            ink_max=ink_max,
            render_dpi=render_dpi,
        )

    doc.close()

    skipped = sum(1 for a in results.values() if a.is_image_only)
    if skipped:
        logger.info(f"页面预检完成: {total} 页中检测到 {skipped} 个图片页")

    return results
