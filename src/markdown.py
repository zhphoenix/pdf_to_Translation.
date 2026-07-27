"""
Markdown 格式化模块
从结构化 OCR 元素生成标准 Markdown
"""

import re
from typing import List, Optional

from . import config
from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()


class MarkdownGenerator:
    """从结构化元素生成 Markdown"""

    def __init__(self):
        """初始化"""
        self.page_separator = config.get("output.page_separator", True)

    def generate_page(
        self, elements: List[OCRElement], page_num: Optional[int] = None
    ) -> str:
        """
        从单页元素生成 Markdown

        Args:
            elements: 单页元素列表
            page_num: 页码

        Returns:
            Markdown 文本
        """
        lines = []

        for elem in elements:
            md_line = self._element_to_markdown(elem)
            if md_line:
                lines.append(md_line)

        text = '\n\n'.join(lines)

        # 添加页面分隔符
        if self.page_separator and page_num is not None:
            text = f"---\n\nPage {page_num}\n\n---\n\n{text}"

        return text

    def generate_document(
        self, pages: List[List[OCRElement]], start_page: int = 1
    ) -> str:
        """
        从多页元素生成完整 Markdown 文档

        Args:
            pages: 每页的元素列表
            start_page: 起始页码

        Returns:
            完整 Markdown 文档
        """
        formatted_pages = []

        for i, page_elements in enumerate(pages):
            page_num = start_page + i
            page_md = self.generate_page(page_elements, page_num)
            if page_md.strip():
                formatted_pages.append(page_md)

        document = '\n\n'.join(formatted_pages)

        # 最终清理
        document = self._final_cleanup(document)

        return document

    def _element_to_markdown(self, elem: OCRElement) -> str:
        """
        将单个元素转换为 Markdown

        Args:
            elem: OCR 元素

        Returns:
            Markdown 文本
        """
        text = elem.text.strip()
        if not text:
            return ""

        elem_type = elem.type.lower()

        if elem_type == "title":
            return f"# {text}"

        elif elem_type == "heading":
            return f"## {text}"

        elif elem_type == "subtitle":
            return f"### {text}"

        elif elem_type == "caption":
            return f"> {text}"

        elif elem_type == "list_item":
            return f"- {text}"

        elif elem_type == "table_cell":
            # 表格单元格单独处理
            return f"| {text} |"

        elif elem_type == "quote":
            return f"> {text}"

        elif elem_type == "text":
            return text

        else:
            # 未知类型，作为普通文本
            return text

    def _final_cleanup(self, text: str) -> str:
        """最终清理"""
        # 移除多余空行（最多保留两个连续空行）
        text = re.sub(r'\n{4,}', '\n\n\n', text)

        # 确保文件以换行符结尾
        if text and not text.endswith('\n'):
            text += '\n'

        return text


def create_markdown_generator() -> MarkdownGenerator:
    """
    创建 Markdown 生成器

    Returns:
        MarkdownGenerator 实例
    """
    return MarkdownGenerator()
