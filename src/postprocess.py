"""
后处理模块（轻量版）
VLM 已输出干净文本，仅做基本过滤：
- 按类型过滤（页眉、页脚、页码）
- 图表噪声过滤（纯数字重复数据）
"""

from typing import List

from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()

# 图表噪声阈值
NOISE_MIN_LENGTH = 200        # 仅过滤长度超过此值的元素
NOISE_MAX_DIGIT_RATIO = 0.60  # 数字字符占比超过此值视为噪声


class StructuredPostProcessor:
    """轻量后处理器"""

    def __init__(self):
        """初始化后处理器"""
        pass

    def process_page(self, elements: List[OCRElement]) -> List[OCRElement]:
        """处理单页元素：过滤图表噪声"""
        result = []
        for e in elements:
            if self._is_chart_noise(e):
                logger.debug(f"过滤图表噪声: {len(e.text)}字, 数字占比{self._digit_ratio(e.text):.0%}")
                continue
            result.append(e)
        return result

    @staticmethod
    def _digit_ratio(text: str) -> float:
        """计算文本中数字字符占比"""
        if not text:
            return 0.0
        digits = sum(1 for c in text if c.isdigit())
        return digits / len(text)

    @classmethod
    def _is_chart_noise(cls, element: OCRElement) -> bool:
        """判断元素是否为图表噪声（纯数字重复数据）"""
        text = element.text.strip()
        if len(text) < NOISE_MIN_LENGTH:
            return False
        return cls._digit_ratio(text) > NOISE_MAX_DIGIT_RATIO

    def process_document(
        self, pages: List[List[OCRElement]]
    ) -> List[List[OCRElement]]:
        """处理整个文档（直接返回）"""
        return pages


def create_post_processor() -> StructuredPostProcessor:
    """创建后处理器实例"""
    return StructuredPostProcessor()
