"""
翻译模块（可选）
将 OCR 结构化元素的文本内容翻译为目标语言
"""

from typing import List, Optional

from openai import OpenAI

from . import config
from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()

# 需要翻译的元素类型
TRANSLATABLE_TYPES = {"text", "title", "heading", "subtitle", "caption", "quote"}

# 翻译 Prompt
TRANSLATE_PROMPT = """You are a professional translator.

Translate the following text to {target_language}.

Rules:
1. Keep proper nouns, brand names, and technical terms as-is when appropriate
2. Maintain the original tone and style
3. Do NOT add explanations or notes
4. Do NOT change formatting markers
5. Output ONLY the translated text, nothing else

Text to translate:

"""


class Translator:
    """翻译器"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        target_language: Optional[str] = None,
        enabled: Optional[bool] = None,
        batch_size: Optional[int] = None
    ):
        """
        初始化翻译器

        Args:
            api_base: LLM API 地址
            model: 模型名称
            timeout: 超时时间
            target_language: 目标语言
            enabled: 是否启用
            batch_size: 批量翻译大小
        """
        self.enabled = enabled if enabled is not None else config.get("translation.enabled", False)

        if not self.enabled:
            logger.info("翻译模块已禁用")
            return

        self.api_base = api_base or config.get("translation.api_base", "http://localhost:8080/v1")
        self.model = model or config.get("translation.model", "qwen3-8b")
        self.timeout = timeout or config.get("translation.timeout", 120)
        self.target_language = target_language or config.get("translation.target_language", "Chinese")
        self.batch_size = batch_size or config.get("translation.batch_size", 10)

        self.client = OpenAI(
            base_url=self.api_base,
            api_key="not-needed",
            timeout=self.timeout
        )

        logger.info(f"翻译器初始化: {self.api_base}, 模型: {self.model}, 目标: {self.target_language}")

    def translate_text(self, text: str) -> str:
        """
        翻译单段文本

        Args:
            text: 源文本

        Returns:
            翻译后的文本
        """
        if not self.enabled or not text.strip():
            return text

        prompt = TRANSLATE_PROMPT.format(target_language=self.target_language) + text

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=4096,
                temperature=0.3
            )

            result = response.choices[0].message.content
            return result.strip() if result else text

        except Exception as e:
            logger.warning(f"翻译失败，返回原文: {e}")
            return text

    def translate_batch(self, texts: List[str]) -> List[str]:
        """
        批量翻译（合并为一次请求）

        Args:
            texts: 文本列表

        Returns:
            翻译后的文本列表
        """
        if not self.enabled or not texts:
            return texts

        # 用编号分隔多段文本
        numbered = "\n\n".join(
            f"[{i+1}] {text}" for i, text in enumerate(texts)
        )

        prompt = f"""You are a professional translator.

Translate each numbered text to {self.target_language}.

Rules:
1. Keep the [N] numbering format
2. Keep proper nouns and brand names when appropriate
3. Output ONLY the translated texts with their numbers
4. Do NOT add explanations

Texts:

{numbered}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0.3
            )

            result = response.choices[0].message.content
            if not result:
                return texts

            # 解析编号结果
            return self._parse_numbered_result(result, len(texts), texts)

        except Exception as e:
            logger.warning(f"批量翻译失败: {e}")
            return texts

    def _parse_numbered_result(
        self, result: str, expected_count: int, original: List[str]
    ) -> List[str]:
        """解析编号格式的翻译结果"""
        import re

        translated = list(original)  # 默认保留原文
        pattern = re.compile(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)', re.DOTALL)

        for match in pattern.finditer(result):
            idx = int(match.group(1)) - 1
            text = match.group(2).strip()
            if 0 <= idx < expected_count and text:
                translated[idx] = text

        return translated

    def translate_elements(self, elements: List[OCRElement]) -> List[OCRElement]:
        """
        翻译元素列表中的可翻译文本

        Args:
            elements: OCR 元素列表

        Returns:
            翻译后的元素列表（结构不变，文本替换）
        """
        if not self.enabled:
            return elements

        # 收集需要翻译的元素索引
        translatable_indices = [
            i for i, elem in enumerate(elements)
            if elem.type in TRANSLATABLE_TYPES and elem.text.strip()
        ]

        if not translatable_indices:
            return elements

        logger.info(f"翻译 {len(translatable_indices)} 个文本元素...")

        # 分批翻译
        for batch_start in range(0, len(translatable_indices), self.batch_size):
            batch_indices = translatable_indices[batch_start:batch_start + self.batch_size]
            batch_texts = [elements[i].text for i in batch_indices]

            # 批量翻译
            translated_texts = self.translate_batch(batch_texts)

            # 更新元素
            for idx, translated in zip(batch_indices, translated_texts):
                elements[idx] = OCRElement(
                    type=elements[idx].type,
                    bbox=elements[idx].bbox,
                    text=translated,
                    page=elements[idx].page
                )

            logger.info(
                f"翻译进度: {min(batch_start + self.batch_size, len(translatable_indices))}"
                f"/{len(translatable_indices)}"
            )

        return elements

    def translate_pages(
        self, pages: List[List[OCRElement]]
    ) -> List[List[OCRElement]]:
        """
        翻译多页元素

        Args:
            pages: 每页元素列表

        Returns:
            翻译后的每页元素列表
        """
        if not self.enabled:
            return pages

        translated_pages = []
        total = len(pages)

        for i, page_elements in enumerate(pages):
            logger.info(f"翻译页面: {i + 1}/{total}")
            translated = self.translate_elements(page_elements)
            translated_pages.append(translated)

        return translated_pages


def create_translator(enabled: Optional[bool] = None) -> Translator:
    """
    创建翻译器实例

    Args:
        enabled: 是否启用（覆盖配置）

    Returns:
        Translator 实例
    """
    return Translator(enabled=enabled)
