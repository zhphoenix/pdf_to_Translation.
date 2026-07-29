"""
页面过滤模块（极简版）

VLM 引擎本身足够智能，能识别并跳过广告/图表/页眉页脚。
本模块只负责跳过封面页（杂志前 N 页通常是封面/内封/空白页）。

判断依据：
- 前 skip_front_pages 页直接跳过（可配置，默认 2）
- 其余页面全部交给 VLM 处理
"""

from dataclasses import dataclass
from typing import Dict

from . import config
from .utils import get_logger

logger = get_logger()


@dataclass
class PageAnalysis:
    """单页分析结果"""
    page_num: int                 # 页码（0 开始）
    is_image_only: bool           # 是否应跳过
    reason: str                   # 判定原因


def prescan_pages(pdf_path) -> Dict[int, PageAnalysis]:
    """
    预扫描 PDF，返回应跳过的页面集合。

    当前策略：跳过前 N 页（封面/内封）。

    Args:
        pdf_path: PDF 文件路径

    Returns:
        {page_num: PageAnalysis} 需要跳过的页面字典
    """
    import fitz

    skip_front = config.get("page_filter.skip_front_pages", 2)

    results: Dict[int, PageAnalysis] = {}

    doc = fitz.open(str(pdf_path))
    total = len(doc)
    doc.close()

    if skip_front <= 0:
        return results

    # 跳过前 N 页（封面）
    actual_skip = min(skip_front, total)
    for i in range(actual_skip):
        results[i] = PageAnalysis(
            page_num=i,
            is_image_only=True,
            reason=f"cover page (front {i + 1})",
        )

    if actual_skip > 0:
        logger.info(f"页面过滤: 跳过前 {actual_skip} 页（封面）")

    return results
