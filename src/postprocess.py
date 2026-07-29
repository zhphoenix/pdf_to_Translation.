"""
后处理模块（轻量版）
VLM 已输出干净文本，仅做基本过滤：
- 按类型过滤（页眉、页脚、页码）
"""

from typing import List

from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()


class StructuredPostProcessor:
    """轻量后处理器"""

    def __init__(self):
        """初始化后处理器"""
        pass

    def process_page(self, elements: List[OCRElement]) -> List[OCRElement]:
        """处理单页元素（直接返回，VLM 输出已足够干净）"""
        return elements

    def process_document(
        self, pages: List[List[OCRElement]]
    ) -> List[List[OCRElement]]:
        """处理整个文档（直接返回）"""
        return pages


def create_post_processor() -> StructuredPostProcessor:
    """创建后处理器实例"""
    return StructuredPostProcessor()
