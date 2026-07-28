"""
翻译模块（可选）
将 OCR 结构化元素的文本内容翻译为目标语言
支持两种输出模式：
- 结构化翻译：翻译后保留 OCRElement 结构（用于后续 Markdown 生成）
- 纯文本翻译：直接输出译文纯文本（评估翻译质量用）
"""

from typing import List, Optional

from openai import OpenAI

from . import config
from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()

# 需要翻译的元素类型
TRANSLATABLE_TYPES = {"text", "title", "heading", "subtitle", "caption", "quote",
                      "image_caption", "image_footnote", "table"}

# 专业英译中 Prompt（针对杂志/经济/时政类内容优化）
TRANSLATE_PROMPT = """你是一位资深英译中翻译专家，擅长《经济学人》《金融时报》等高端英文杂志的中文翻译。

请将以下英文翻译为{target_language}。

翻译要求：
1. 译文流畅自然，符合中文表达习惯，避免翻译腔
2. 专有名词（人名、地名、机构名）首次出现时附注英文原文，之后直接使用中文
3. 经济/金融/政治术语使用业界通用译法
4. 保持原文的语气、修辞和文风（包括反讽、双关、比喻）
5. 仅输出译文，禁止任何前缀、说明、注释或元评论
6. 若原文包含乱码或无法理解，直接输出原文，禁止自我介绍或评论原文质量
7. 若原文存在重复句子或段落（OCR 错误），静默去除重复，只翻译一次，禁止添加任何说明
8. 若原文有多处高度相似的内容，合并为一段连贯译文，禁止注明“已合并”或“已去重”

重要：无论原文有什么问题，只输出最终译文，不要添加任何括号内的说明或解释。

待翻译文本：

"""

# 批量翻译 Prompt
BATCH_TRANSLATE_PROMPT = """你是一位资深英译中翻译专家，擅长《经济学人》等高端英文杂志的中文翻译。

请将以下编号文本逐条翻译为{target_language}。

翻译要求：
1. 译文流畅自然，符合中文表达习惯，避免翻译腔
2. 经济/金融/政治术语使用业界通用译法
3. 保持原文语气和文风
4. 严格保留 [N] 编号格式（如 [1] [2] [3]），每条译文紧跟编号，编号独占一行
5. 必须翻译全部 {count} 条文本，不得遗漏
6. 仅输出译文，禁止任何前缀、说明、注释或元评论
7. 若原文包含乱码或无法理解，直接输出原文，禁止自我介绍或评论原文质量
8. 若原文存在重复句子或段落（OCR 错误），静默去除重复，只翻译一次
9. 若原文有多处高度相似的内容，合并为一段连贯译文，禁止注明“已合并”

重要：只输出最终译文，不要添加任何括号内的说明或解释。

待翻译文本：

{numbered}"""


class Translator:
    """翻译器"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        target_language: Optional[str] = None,
        enabled: Optional[bool] = None,
        batch_size: Optional[int] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        repeat_penalty: Optional[float] = None
    ):
        """
        初始化翻译器

        Args:
            api_base: LLM API 地址
            api_key: API 密钥（云端模型需要，本地模型可省略）
            model: 模型名称
            timeout: 超时时间
            target_language: 目标语言
            enabled: 是否启用
            batch_size: 批量翻译大小
            max_tokens: 单次请求最大生成 token
            temperature: 生成温度
            repeat_penalty: 重复惩罚系数（1.0=无惩罚，>1.0 抑制重复）
        """
        self.enabled = enabled if enabled is not None else config.get("translation.enabled", False)

        if not self.enabled:
            logger.info("翻译模块已禁用")
            return

        self.api_base = api_base or config.get("translation.api_base", "http://localhost:8080/v1")
        self.api_key = api_key or config.get("translation.api_key", "not-needed")
        self.model = model or config.get("translation.model", "sisyphus")
        self.timeout = timeout or config.get("translation.timeout", 300)
        self.target_language = target_language or config.get("translation.target_language", "简体中文")
        self.batch_size = batch_size or config.get("translation.batch_size", 5)
        self.max_tokens = max_tokens or config.get("translation.max_tokens", 16384)
        self.temperature = temperature if temperature is not None else config.get("translation.temperature", 0.3)
        self.repeat_penalty = repeat_penalty if repeat_penalty is not None else config.get("translation.repeat_penalty", 1.1)
        self.disable_thinking = config.get("translation.disable_thinking", True)  # 翻译任务关闭推理思考

        # 判断是否为本地模型（本地 llama.cpp 不支持 extra_body 参数）
        self.is_local = self.api_key == "not-needed" or self.api_key == "EMPTY"

        self.client = OpenAI(
            base_url=self.api_base,
            api_key=self.api_key,
            timeout=self.timeout
        )

        logger.info(f"翻译器初始化: {self.api_base}, 模型: {self.model}, "
                    f"目标: {self.target_language}, 本地: {self.is_local}")

    def _call_api(self, prompt: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """
        调用 LLM API，支持推理模型（reasoning_content + content）。
        当 content 为空（推理消耗所有 token）时自动重试并加大 max_tokens。

        Returns:
            翻译结果文本，失败返回 None
        """
        tokens = max_tokens or self.max_tokens
        max_retries = 2  # content 为空时最多重试 1 次

        for attempt in range(max_retries + 1):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": tokens,
                    "temperature": self.temperature,
                }
                if self.is_local:
                    extra = {"repeat_penalty": self.repeat_penalty}
                    if self.disable_thinking:
                        extra["chat_template_kwargs"] = {"enable_thinking": False}
                    kwargs["extra_body"] = extra

                response = self.client.chat.completions.create(**kwargs)

                msg = response.choices[0].message

                # 提取 content（推理模型的译文在 content 字段）
                content = msg.content

                # 提取 reasoning_content（思考过程）用于日志
                reasoning = None
                if hasattr(msg, 'model_extra') and msg.model_extra:
                    reasoning = msg.model_extra.get('reasoning_content')

                # 记录 token 使用
                usage = response.usage
                if usage:
                    logger.debug(
                        f"API tokens: prompt={usage.prompt_tokens}, "
                        f"completion={usage.completion_tokens}, "
                        f"reasoning={len(reasoning) if reasoning else 0}chars"
                    )

                # content 非空 → 成功
                if content and content.strip():
                    return content.strip()

                # content 为空 → 推理消耗了所有 token
                if reasoning:
                    logger.warning(
                        f"content 为空（推理消耗 {usage.completion_tokens if usage else '?'} tokens），"
                        f"reasoning={len(reasoning)}chars"
                    )
                else:
                    logger.warning("content 为空且无 reasoning_content")

                # 重试：加大 max_tokens
                if attempt < max_retries:
                    tokens = min(tokens * 2, 32768)
                    logger.info(f"重试: max_tokens 加倍至 {tokens}")
                    continue

                return None

            except Exception as e:
                logger.warning(f"API 调用失败: {e}")
                return None

        return None

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

        result = self._call_api(prompt)
        return result if result else text

    def translate_batch(self, texts: List[str]) -> List[str]:
        """
        批量翻译（合并为一次请求）
        批量失败时自动降级为逐条翻译；解析不完整的条目自动补译。

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

        prompt = BATCH_TRANSLATE_PROMPT.format(
            target_language=self.target_language,
            numbered=numbered,
            count=len(texts)
        )

        try:
            result = self._call_api(
                prompt,
                max_tokens=self.max_tokens  # 批量翻译可能需要更多 token
            )
            if not result:
                logger.warning(f"批量翻译返回空结果，共 {len(texts)} 条，降级为逐条翻译")
                return self._fallback_individual(texts)

            # 解析编号结果
            translated = self._parse_numbered_result(result, len(texts), texts)

            # 检查是否有未成功翻译的项（保留了原文），逐条补译
            for i, (orig, trans) in enumerate(zip(texts, translated)):
                if orig.strip() and trans.strip() == orig.strip():
                    logger.info(f"批量翻译第 {i+1} 条未解析到，逐条补译...")
                    translated[i] = self.translate_text(orig)

            return translated

        except Exception as e:
            logger.warning(f"批量翻译失败 ({len(texts)} 条): {e}")
            logger.info("降级为逐条翻译...")
            return self._fallback_individual(texts)

    def _fallback_individual(self, texts: List[str]) -> List[str]:
        """批量失败时逐条翻译的降级策略"""
        results = []
        for i, text in enumerate(texts):
            try:
                result = self.translate_text(text)
                results.append(result)
            except Exception as e:
                logger.warning(f"逐条翻译第 {i+1} 条也失败，保留原文: {e}")
                results.append(text)
        return results

    def _parse_numbered_result(
        self, result: str, expected_count: int, original: List[str]
    ) -> List[str]:
        """解析编号格式的翻译结果，支持多种编号格式"""
        import re

        translated = list(original)  # 默认保留原文

        # 支持多种编号格式: [1], 1., 1), **1.**, #1
        patterns = [
            r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)',        # [N]
            r'\*\*(\d+)[.\)]\*\*\s*(.*?)(?=\*\*\d+|$)', # **N.**
            r'(^|\n)\s*(\d+)[.)]\s+(.*?)(?=\n\s*\d+[.)]\s|$)', # N. or N)
            r'(^|\n)\s*#(\d+)\s*(.*?)(?=\n\s*#\d+|$)',  # #N
        ]

        matched_indices = set()

        for pattern_str in patterns:
            pattern = re.compile(pattern_str, re.DOTALL)
            for match in pattern.finditer(result):
                try:
                    idx = int(match.group(2 if match.lastindex >= 3 else 1)) - 1
                    text = match.group(match.lastindex).strip()
                    if 0 <= idx < expected_count and text and idx not in matched_indices:
                        translated[idx] = text
                        matched_indices.add(idx)
                except (IndexError, ValueError):
                    continue
            if len(matched_indices) == expected_count:
                break  # 全部匹配到，无需尝试其他格式

        matched = len(matched_indices)
        if matched < expected_count:
            missing = [i+1 for i in range(expected_count) if i not in matched_indices]
            logger.warning(
                f"批量翻译解析不完整: 期望 {expected_count} 条，解析到 {matched} 条。"
                f"缺失: {missing}"
            )

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

    def translate_pages_to_text(
        self, pages: List[List[OCRElement]]
    ) -> str:
        """
        翻译多页元素并直接输出纯文本（不生成 Markdown）

        用于评估 OCR 输出直接翻译的质量。
        每页之间用空行分隔，每个元素独占一段。

        Args:
            pages: 每页元素列表

        Returns:
            翻译后的纯文本字符串
        """
        if not self.enabled:
            # 未启用翻译时，直接拼接原文
            return self._pages_to_plain_text(pages)

        translated_pages = self.translate_pages(pages)
        return self._pages_to_plain_text(translated_pages)

    @staticmethod
    def _pages_to_plain_text(pages: List[List[OCRElement]]) -> str:
        """
        将多页元素拼接为纯文本

        规则：
        - 标题类元素前后加空行
        - 普通文本元素之间用换行分隔
        - 页面之间用双换行分隔
        """
        page_texts = []

        for page_elements in pages:
            lines = []
            for elem in page_elements:
                text = elem.text.strip()
                if not text:
                    continue
                # 标题类元素加空行突出
                if elem.type in ("title", "heading", "subtitle"):
                    lines.append(f"\n{text}\n")
                else:
                    lines.append(text)
            page_texts.append("\n".join(lines))

        return "\n\n".join(page_texts)


def create_translator(enabled: Optional[bool] = None) -> Translator:
    """
    创建翻译器实例

    Args:
        enabled: 是否启用（覆盖配置）

    Returns:
        Translator 实例
    """
    return Translator(enabled=enabled)
