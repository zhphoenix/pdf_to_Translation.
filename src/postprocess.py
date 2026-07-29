"""
后处理模块
基于 OCR 结构化元素进行后处理：
- 按类型过滤（页眉、页脚、页码、广告）
- 按坐标合并相邻文本块
- 保留标题、段落、列表、表格结构
"""

import re
from collections import Counter
from typing import List, Optional, Set, Tuple

from . import config
from .utils import get_logger
from .ocr import OCRElement

logger = get_logger()

# 需要过滤的元素类型
FILTER_TYPES = {"header", "footer", "page_number", "image_footnote"}


class StructuredPostProcessor:
    """结构化后处理器"""

    # OCR 模型系统提示词泄漏特征（幻觉文本开头）
    HALLUCINATION_PREFIXES = (
        "The Ground Truth image displays",
        "The image contains no text",
        "According to Rule",
        "The following table provides",    # OCR 元数据注释泄漏
        "The second line is a stylistic",  # OCR 排版分析泄漏
        "The English text in the source image is a single",  # OCR 图像描述泄漏
        "The best answer in this case is",     # 无关问答文本泄漏
    )

    # OCR 幻觉模式正则（t2t2 重复 token、荒谬大数字等）
    HALLUCINATION_PATTERNS = [
        re.compile(r'^t2t2[,0]*$'),                          # t2t2,000,000,... 纯幻觉 token
        re.compile(r'^#\s*t2t2'),                             # # t2t2,... 被标记为标题
        re.compile(r't2t2[,\d]{20,}'),                        # 包含超长 t2t2 数字串
        re.compile(r'^10\^[2-9]\d$'),                         # 10^36, 10^42 等荒谬大指数
        re.compile(r'^[\d.]+\s*[×xX]\s*10[³²⁶⁸⁰⁴⁷⁹\^]'),    # 2.2×10³⁶ 等
        re.compile(r'^[\d.]+\s*[×xX]\s*10\^\d{2,}'),        # 2.2×10^36 等
        re.compile(r'^\|?\|+$'),                               # |||| 纯竖线
        re.compile(r'^[\d,]{50,}$'),                           # 超长纯数字串（50+字符）
        re.compile(r'^(\S+)(?:\s+\1){5,}\s*$'),              # 同一 token 重复 6+ 次（如 "1 1 1 1..."）
        re.compile(r'(\[Non-Text\]\s*){3,}', re.IGNORECASE), # [Non-Text] 标记重复 3+ 次
        re.compile(r'^\[No text detected\]\s*$', re.IGNORECASE),  # [No text detected] 占位符
        re.compile(r'The following table provides the original text', re.IGNORECASE),  # OCR 元数据注释泄漏
        re.compile(r'^(?:\d+\.\s*){10,}\s*$'),                     # 纯编号序列（如 "1. 2. 3. ... 99."）
        re.compile(r'^1\.\s+\d{4}[,年]', re.MULTILINE),             # 编号事实幻觉（"1. 2017..." 重复以 1. 开头）
    ]

    def __init__(self):
        """初始化后处理器"""
        self.remove_headers = config.get("postprocess.remove_headers", True)
        self.remove_footers = config.get("postprocess.remove_footers", True)
        self.remove_page_numbers = config.get("postprocess.remove_page_numbers", True)
        self.remove_tables = config.get("postprocess.remove_tables", True)
        self.remove_ads = config.get("postprocess.remove_ads", True)
        self.merge_broken_lines = config.get("postprocess.merge_broken_lines", True)

        # 质量过滤参数
        self.quality_filter_enabled = config.get("postprocess.quality_filter.enabled", True)
        self.repeat_min_length = config.get("postprocess.quality_filter.repeat_min_length", 500)  # 降低阈值捕获短文本重复
        self.repeat_max_ratio = config.get("postprocess.quality_filter.repeat_max_ratio", 0.5)
        self.repeat_ngram_size = config.get("postprocess.quality_filter.repeat_ngram_size", 15)
        self.sentence_repeat_threshold = config.get("postprocess.quality_filter.sentence_repeat_threshold", 3)  # 同一句子出现N次触发截断
        self.dedup_similarity_threshold = config.get("postprocess.quality_filter.dedup_similarity_threshold", 0.85)  # 元素去重相似度阈值
        self.hallucination_prefixes = tuple(
            config.get("postprocess.quality_filter.hallucination_prefixes",
                       list(self.HALLUCINATION_PREFIXES))
        )
        self.hallucination_patterns = [
            re.compile(p.pattern, p.flags)
            for p in self.HALLUCINATION_PATTERNS
        ]

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
        # 0. 质量过滤（重复退化 + 幻觉检测 + 句子重复检测）
        if self.quality_filter_enabled:
            elements = self._quality_filter_page(elements)

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

        # 5. 元素级去重（检测相似/重复的元素）
        if self.quality_filter_enabled:
            elements = self._deduplicate_elements(elements)

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
            etype = elem.type
            # 过滤页眉
            if self.remove_headers and etype == "header":
                continue
            # 过滤页脚
            if self.remove_footers and etype == "footer":
                continue
            # 过滤页码
            if self.remove_page_numbers and etype == "page_number":
                continue
            # 过滤 HTML 表格（OCR 输出的表格质量通常较差）
            if self.remove_tables and etype == "table":
                continue
            # 过滤图片脚注（OCR 幻觉高发区：编号序列、en-dash 年份重复）
            if etype == "image_footnote":
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

    # ─── 质量过滤 ─────────────────────────────────────────────

    def _quality_filter_page(self, elements: List[OCRElement]) -> List[OCRElement]:
        """
        OCR 输出质量过滤：检测并清理重复退化和幻觉文本

        Args:
            elements: OCR 元素列表

        Returns:
            过滤后的元素列表
        """
        result = []
        page = elements[0].page if elements else 0

        for elem in elements:
            text = elem.text.strip()
            if not text:
                result.append(elem)
                continue

            # 检测1: 幻觉文本（OCR 模型系统提示词泄漏）
            if self._is_hallucination(text):
                logger.warning(
                    f"[质量过滤] Page {page}: 移除幻觉文本 "
                    f"(len={len(text)}, 开头: {text[:60]}...)"
                )
                continue  # 直接丢弃

            # 检测2: 重复退化（n-gram 唯一比过低）
            if len(text) > self.repeat_min_length:
                ratio = self._ngram_unique_ratio(text, self.repeat_ngram_size)
                if ratio < self.repeat_max_ratio:
                    truncated = self._truncate_repetition(text)
                    if truncated and len(truncated) > 50:
                        logger.warning(
                            f"[质量过滤] Page {page}: 重复退化截断 "
                            f"(原长={len(text):,}, ratio={ratio:.4f}, "
                            f"截断后={len(truncated):,})"
                        )
                        elem = OCRElement(
                            type=elem.type, bbox=elem.bbox,
                            text=truncated, page=elem.page
                        )
                    else:
                        logger.warning(
                            f"[质量过滤] Page {page}: 移除重复退化文本 "
                            f"(原长={len(text):,}, ratio={ratio:.4f}, 截断后过短)"
                        )
                        continue  # 截断后太短，丢弃

            # 检测3: 荒谬未来日期（OCR 幻觉生成年份序列到 2100+）
            if self._has_absurd_future_dates(text):
                logger.warning(
                    f"[质量过滤] Page {page}: 移除荒谬未来日期文本 "
                    f"(len={len(text)}, 开头: {text[:60]}...)"
                )
                continue

            # 检测4: 年份区间序列幻觉（如 2017-18, 2018-19, ... 2099-100）
            yr_truncated = self._truncate_year_range_hallucination(text)
            if yr_truncated is not None:
                logger.warning(
                    f"[质量过滤] Page {page}: 截断年份区间序列幻觉 "
                    f"(原长={len(text)}, 截断后={len(yr_truncated)})"
                )
                elem = OCRElement(
                    type=elem.type, bbox=elem.bbox,
                    text=yr_truncated, page=elem.page
                )

            # 检测5: 段落内句子重复（同一段落被复制两遍）
            cur_text = elem.text.strip()
            sr_truncated = self._truncate_sentence_repeats(cur_text)
            if sr_truncated is not None:
                logger.warning(
                    f"[质量过滤] Page {page}: 截断重复段落 "
                    f"(原长={len(cur_text)}, 截断后={len(sr_truncated)})"
                )
                elem = OCRElement(
                    type=elem.type, bbox=elem.bbox,
                    text=sr_truncated, page=elem.page
                )

            # 检测6: 短语级重复（同一短语重复 8+ 次）
            cur_text = elem.text.strip()
            pr_truncated = self._truncate_phrase_repetition(cur_text)
            if pr_truncated is not None:
                logger.warning(
                    f"[质量过滤] Page {page}: 截断短语重复 "
                    f"(原长={len(cur_text)}, 截断后={len(pr_truncated)})"
                )
                elem = OCRElement(
                    type=elem.type, bbox=elem.bbox,
                    text=pr_truncated, page=elem.page
                )

            result.append(elem)

        return result

    def _truncate_phrase_repetition(self, text: str) -> Optional[str]:
        """
        检测短语级重复并在重复起点截断。

        OCR 幻觉特征：同一短语（3-8词）在文本中重复出现 8+ 次，
        且高频短语占总 token 数超过 40%。

        Returns:
            截断后的文本，或 None（无幻觉）。
        """
        words = text.split()
        if len(words) < 20:
            return None

        # 检查 3-8 词短语
        for phrase_len in range(3, 9):
            phrases = []
            for i in range(len(words) - phrase_len + 1):
                phrase = ' '.join(words[i:i + phrase_len]).lower()
                phrases.append(phrase)
            if not phrases:
                continue

            counter = Counter(phrases)
            most_common_phrase, count = counter.most_common(1)[0]

            # 同一短语出现 8+ 次，且占 token 数超过 40%
            if count >= 8 and (count * phrase_len / len(words)) > 0.4:
                # 在第一次出现处截断
                first_idx = phrases.index(most_common_phrase)
                truncated = ' '.join(words[:first_idx])
                if len(truncated) >= 50:
                    return truncated
        return None

    def _is_hallucination(self, text: str) -> bool:
        """检测文本是否为 OCR 模型幻觉（系统提示词泄漏或重复退化 token）"""
        if text.startswith(self.hallucination_prefixes):
            return True
        # 检查幻觉模式（t2t2、荒谬大数字等）
        for pattern in self.hallucination_patterns:
            if pattern.search(text):
                return True
        return False

    # 检测荒谬未来年份的正则
    _RE_FUTURE_YEAR = re.compile(r'\b(2[1-9]\d{2})\b')  # 2100-2999
    # 检测未来十年格式的正则（如 "the 2100s"）
    _RE_FUTURE_DECADE = re.compile(r'\bthe (2[1-9]\d{2})s\b')

    def _has_absurd_future_dates(self, text: str) -> bool:
        """
        检测文本是否包含荒谬未来年份序列。
        
        OCR 幻觉特征：生成从当前年份一直递增到 2100+ 的日期序列。
        判定规则：如果文本中包含 3 个以上 2100+ 的年份（含十年格式），视为幻觉。
        """
        if len(text) < 200:
            return False
        future_years = self._RE_FUTURE_YEAR.findall(text)
        future_decades = self._RE_FUTURE_DECADE.findall(text)
        return (len(future_years) + len(future_decades)) >= 3

    # 检测年份区间序列的正则（如 "2017-18", "2098-99"）
    _RE_YEAR_RANGE = re.compile(r'\b\d{4}-\d{2,4}\b')
    # 检测十年序列的正则（如 "the 1980s, the 1990s, ..."）
    _RE_DECADE_SEQ = re.compile(r'\bthe \d{4}s\b')

    def _truncate_year_range_hallucination(self, text: str) -> Optional[str]:
        """
        检测年份区间/十年序列幻觉并在幻觉起点截断。

        OCR 幻觉特征：生成连续递增的年份区间序列（如 2017-18, 2018-19, ... 2099-100）
        或十年序列（如 the 1980s, the 1990s, ... the 2999s）。
        判定规则：文本中出现 5+ 年份区间 或 8+ 十年模式。

        Returns:
            截断后的文本（保留幻觉前的有效内容），或 None（无幻觉或截断后过短）。
        """
        yr_matches = list(self._RE_YEAR_RANGE.finditer(text))
        decade_matches = list(self._RE_DECADE_SEQ.finditer(text))
        # 任一模式达到阈值即触发截断
        yr_triggered = len(yr_matches) >= 5
        dec_triggered = len(decade_matches) >= 8
        if not yr_triggered and not dec_triggered:
            return None
        # 在最早的幻觉起点处截断
        candidates = []
        if yr_triggered:
            candidates.append(yr_matches[0].start())
        if dec_triggered:
            candidates.append(decade_matches[0].start())
        cut_pos = min(candidates)
        truncated = text[:cut_pos].rstrip()
        if len(truncated) < 50:
            return None
        return truncated

    def _truncate_sentence_repeats(self, text: str) -> Optional[str]:
        """
        检测段落内句子重复并在重复起点截断。

        OCR 幻觉特征：同一段落在文本中被完整复制两遍。
        判定规则：超过 30% 的长句子（>30字符）出现 2 次以上。

        Returns:
            截断后的文本（保留到第一次重复前），或 None（无重复）。
        """
        sentences = re.split(r'(?<=[.!?])\s+', text)
        if len(sentences) < 4:
            return None

        # 统计长句子出现次数
        seen = {}
        for i, s in enumerate(sentences):
            key = s.strip()[:80]
            if len(key) < 30:
                continue
            if key not in seen:
                seen[key] = []
            seen[key].append(i)

        # 检查是否有重复的长句子
        repeated = {k: v for k, v in seen.items() if len(v) >= 2}
        if not repeated:
            return None

        # 计算重复比例
        total_long = sum(1 for s in sentences if len(s.strip()) > 30)
        repeated_count = sum(len(v) for v in repeated.values())
        if total_long == 0 or repeated_count / total_long < 0.3:
            return None

        # 找到最早重复句子的第二次出现位置，在此截断
        earliest_second = min(v[1] for v in repeated.values())
        # 找重复块中第一次出现的最大索引（重复块的结束位置）
        max_first = max(v[0] for v in repeated.values())
        # 保留从开头到重复块结束（包含整个第一段）
        cut_sentence_idx = max_first + 1
        # 计算截断位置（字符位置）
        pos = 0
        for i, s in enumerate(sentences):
            if i >= cut_sentence_idx:
                break
            pos += len(s) + 1  # +1 for the separator

        truncated = text[:pos].rstrip()
        if len(truncated) < 50:
            return None
        return truncated

    @staticmethod
    def _ngram_unique_ratio(text: str, n: int) -> float:
        """
        计算 n-gram 唯一比（去重后唯一 n-gram 数 / 总 n-gram 数）

        正常英文文本接近 1.0，重复退化文本接近 0
        """
        if len(text) <= n:
            return 1.0
        ngrams = [text[i:i + n] for i in range(len(text) - n)]
        total = len(ngrams)
        unique = len(set(ngrams))
        return unique / total if total > 0 else 1.0

    @staticmethod
    def _truncate_repetition(text: str, n: int = 20, window: int = 500) -> str:
        """
        截断重复退化文本，保留重复起始前的正常内容

        算法：用滑动窗口检测局部 n-gram 唯一比急剧下降的位置，
        在该位置截断，保留前面的正常内容。

        Args:
            text: 原始文本
            n: 检测重复的 n-gram 大小
            window: 滑动窗口大小（字符数）

        Returns:
            截断后的文本
        """
        if len(text) <= window:
            return text

        # 用滑动窗口检测局部 n-gram 唯一比
        step = window // 2
        window_ratios = []

        for start in range(0, len(text) - window, step):
            chunk = text[start:start + window]
            ngrams = [chunk[i:i + n] for i in range(len(chunk) - n)]
            if not ngrams:
                continue
            unique = len(set(ngrams))
            ratio = unique / len(ngrams)
            window_ratios.append((start, ratio))

        if not window_ratios:
            return text

        # 找到第一个局部唯一比 < 0.7 的位置（正常英文文本始终 > 0.95）
        cut_pos = None
        for pos, ratio in window_ratios:
            if ratio < 0.7:
                cut_pos = pos
                break

        if cut_pos is None:
            return text

        # 在截断点附近寻找句子边界
        truncated = text[:cut_pos].rstrip()
        for sep in ['. ', '。', '\n', '.\n']:
            last_sep = truncated.rfind(sep)
            if last_sep > len(truncated) * 0.3:
                truncated = truncated[:last_sep + len(sep)]
                break

        return truncated.strip()

    # ─── 元素级去重 ─────────────────────────────────────

    def _deduplicate_elements(self, elements: List[OCRElement]) -> List[OCRElement]:
        """
        元素级去重：检测并移除内容高度相似的重复元素

        算法：对每个元素生成文本指纹，与已保留元素比对，
        相似度超过阈值的视为重复并跳过。

        Args:
            elements: OCR 元素列表

        Returns:
            去重后的元素列表
        """
        if not elements:
            return []

        result = []
        seen_texts = []  # 已保留元素的文本

        for elem in elements:
            text = elem.text.strip()
            if not text or len(text) < 50:  # 短文本不去重（可能是标题等）
                result.append(elem)
                continue

            # 与已保留元素比对
            is_duplicate = False
            for seen_text in seen_texts:
                similarity = self._text_similarity(text, seen_text)
                if similarity >= self.dedup_similarity_threshold:
                    logger.info(
                        f"[去重] 跳过重复元素 (相似度={similarity:.2f}, "
                        f"len={len(text)}, 开头: {text[:40]}...)"
                    )
                    is_duplicate = True
                    break

            if not is_duplicate:
                result.append(elem)
                seen_texts.append(text)

        return result

    @staticmethod
    def _text_similarity(text1: str, text2: str) -> float:
        """
        计算两段文本的相似度（基于字符级 Jaccard 系数）

        Args:
            text1, text2: 待比较的文本

        Returns:
            相似度 [0, 1]，1 表示完全相同
        """
        if text1 == text2:
            return 1.0

        # 用 3-gram 集合计算 Jaccard 相似度
        n = 3
        set1 = {text1[i:i+n] for i in range(len(text1) - n + 1)}
        set2 = {text2[i:i+n] for i in range(len(text2) - n + 1)}

        if not set1 or not set2:
            return 0.0

        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0


def create_post_processor() -> StructuredPostProcessor:
    """
    创建后处理器实例

    Returns:
        StructuredPostProcessor 实例
    """
    return StructuredPostProcessor()

