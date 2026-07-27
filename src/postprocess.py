"""
后处理模块
基于 OCR 结构化元素进行后处理：
- 按类型过滤（页眉、页脚、页码、广告）
- 按坐标合并相邻文本块
- 保留标题、段落、列表、表格结构
"""

import re
from collections import Counter
from typing import List, Set, Tuple

from . import config
from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()

# 需要过滤的元素类型
FILTER_TYPES = {"header", "footer", "page_number"}

# 内容元素类型
CONTENT_TYPES = {"text", "title", "caption", "list_item", "table_cell"}


class StructuredPostProcessor:
    """结构化后处理器"""

    def __init__(self):
        """初始化后处理器"""
        self.remove_headers = config.get("postprocess.remove_headers", True)
        self.remove_footers = config.get("postprocess.remove_footers", True)
        self.remove_page_numbers = config.get("postprocess.remove_page_numbers", True)
        self.remove_ads = config.get("postprocess.remove_ads", True)
        self.merge_broken_lines = config.get("postprocess.merge_broken_lines", True)

        # 广告关键词
        self.ad_patterns = config.get("postprocess.ad_patterns", [
            "Subscribe now",
            "Advertisement"
        ])
        self.ad_regex = [
            re.compile(re.escape(p), re.IGNORECASE) for p in self.ad_patterns
        ]

        # 自定义页眉/页脚正则
        self.header_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in config.get("postprocess.header_patterns", [])
        ]
        self.footer_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in config.get("postprocess.footer_patterns", [])
        ]

        # 合并阈值：y 坐标差距小于此值时合并（像素）
        self.merge_y_threshold = 15

    def process_page(self, elements: List[OCRElement]) -> List[OCRElement]:
        """
        处理单页元素

        Args:
            elements: 单页的 OCR 元素列表

        Returns:
            处理后的元素列表
        """
        # 1. 按类型过滤
        elements = self._filter_by_type(elements)

        # 2. 删除广告
        if self.remove_ads:
            elements = self._remove_ads(elements)

        # 3. 删除自定义页眉/页脚
        elements = self._remove_custom_patterns(elements)

        # 4. 合并相邻文本块
        if self.merge_broken_lines:
            elements = self._merge_adjacent_text(elements)

        return elements

    def process_document(
        self, pages: List[List[OCRElement]]
    ) -> List[List[OCRElement]]:
        """
        处理整个文档

        Args:
            pages: 每页的元素列表

        Returns:
            处理后的每页元素列表
        """
        if not pages:
            return []

        # 跨页检测重复页眉/页脚
        repeated_texts = self._detect_repeated_elements(pages)
        if repeated_texts:
            logger.info(f"检测到跨页重复元素: {len(repeated_texts)} 个")

        processed_pages = []
        for page_elements in pages:
            # 删除跨页重复元素
            page_elements = self._remove_repeated(page_elements, repeated_texts)
            # 单页处理
            page_elements = self.process_page(page_elements)
            processed_pages.append(page_elements)

        return processed_pages

    def _filter_by_type(self, elements: List[OCRElement]) -> List[OCRElement]:
        """按类型过滤元素"""
        result = []

        for elem in elements:
            # 过滤页眉
            if self.remove_headers and elem.type == "header":
                continue
            # 过滤页脚
            if self.remove_footers and elem.type == "footer":
                continue
            # 过滤页码
            if self.remove_page_numbers and elem.type == "page_number":
                continue
            result.append(elem)

        return result

    def _remove_ads(self, elements: List[OCRElement]) -> List[OCRElement]:
        """删除广告元素"""
        if not self.ad_regex:
            return elements

        result = []
        for elem in elements:
            if any(p.search(elem.text) for p in self.ad_regex):
                logger.debug(f"删除广告: {elem.text[:50]}...")
                continue
            result.append(elem)
        return result

    def _remove_custom_patterns(self, elements: List[OCRElement]) -> List[OCRElement]:
        """删除匹配自定义页眉/页脚模式的元素"""
        if not self.header_patterns and not self.footer_patterns:
            return elements

        result = []
        for elem in elements:
            # 检查页眉模式（通常位于页面上部，y1 < 60）
            if self.header_patterns and elem.y1 < 60:
                if any(p.search(elem.text) for p in self.header_patterns):
                    continue
            # 检查页脚模式（通常位于页面下部）
            if self.footer_patterns:
                if any(p.search(elem.text) for p in self.footer_patterns):
                    continue
            result.append(elem)
        return result

    def _detect_repeated_elements(
        self, pages: List[List[OCRElement]]
    ) -> Set[str]:
        """
        检测跨页重复出现的短文本（可能是页眉/页脚）

        Args:
            pages: 所有页面的元素列表

        Returns:
            重复文本集合
        """
        if len(pages) < 3:
            return set()

        text_counter = Counter()

        for page_elements in pages:
            for elem in page_elements:
                # 只统计短文本（页眉/页脚通常较短）
                if len(elem.text) < 80:
                    text_counter[elem.text] += 1

        # 出现在超过 50% 页面中的文本
        threshold = len(pages) * 0.5
        repeated = {
            text for text, count in text_counter.items()
            if count >= threshold
        }

        return repeated

    def _remove_repeated(
        self, elements: List[OCRElement], repeated_texts: Set[str]
    ) -> List[OCRElement]:
        """删除匹配重复文本的元素"""
        if not repeated_texts:
            return elements

        return [
            elem for elem in elements
            if elem.text not in repeated_texts
        ]

    def _merge_adjacent_text(self, elements: List[OCRElement]) -> List[OCRElement]:
        """
        合并相邻的文本块

        规则：
        - 只合并 type == "text" 的相邻元素
        - 同一列（x 坐标接近）
        - y 坐标差距小（同一文本块被分割的情况）
        - 前一段未以句号结尾
        """
        if not elements:
            return []

        result = []
        i = 0

        while i < len(elements):
            elem = elements[i]

            # 只尝试合并 text 类型
            if elem.type != "text":
                result.append(elem)
                i += 1
                continue

            # 尝试与后续 text 元素合并
            merged_text = elem.text
            j = i + 1

            while j < len(elements):
                next_elem = elements[j]

                # 下一个不是 text 类型，停止
                if next_elem.type != "text":
                    break

                # 检查是否在同一列（x1 差距 < 30px）
                if abs(next_elem.x1 - elem.x1) > 30:
                    break

                # 检查 y 坐标是否接近（同一栏内连续）
                y_gap = next_elem.y1 - elem.y2
                if y_gap > self.merge_y_threshold and y_gap > 0:
                    # 间距较大，可能是新段落
                    # 如果前文以句号结尾，认为是新段落
                    if self._ends_sentence(merged_text):
                        break
                    # 否则仍然合并（跨行续句）
                    if y_gap > 50:  # 间距太大，强制分段
                        break

                # 合并
                merged_text = merged_text.rstrip() + ' ' + next_elem.text
                j += 1

            # 创建合并后的元素
            if j > i + 1:
                # 合并了多个元素
                last_elem = elements[j - 1]
                merged_elem = OCRElement(
                    type="text",
                    bbox=(elem.x1, elem.y1, last_elem.x2, last_elem.y2),
                    text=merged_text.strip(),
                    page=elem.page
                )
                result.append(merged_elem)
            else:
                result.append(elem)

            i = j

        return result

    def _ends_sentence(self, text: str) -> bool:
        """判断文本是否以句子结束"""
        if not text:
            return False
        sentence_endings = '.?!。？！'
        return text.rstrip()[-1] in sentence_endings if text.rstrip() else False


def create_post_processor() -> StructuredPostProcessor:
    """
    创建后处理器实例

    Returns:
        StructuredPostProcessor 实例
    """
    return StructuredPostProcessor()

