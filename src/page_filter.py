"""
页面过滤模块

两层过滤策略：
1. 预扫描（OCR 前）：跳过前 N 页（封面/内封），节省 GPU 开销
2. 后分析（OCR 后）：基于 OCR 输出特征检测封面页、广告页、纯表格页

后分析特征规则（基于经济学人 PDF 实测）：
- 封面页：元素 <3 且文本重复率 ≥80%
- 广告页（少元素）：元素 <4 且总文本 <2000 字符（两个条件同时满足）
- 广告页（碎片化）：元素 ≥6 且 ≥80% 元素文字 <100 字符
- 广告页（分类广告）：联系方式模式（Tel/Email/www）≥3 个
- 表格页：元素 <5 且总文本 ≥5000 且行重复率 ≥50%（表头被极度重复）
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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


# ─── OCR 后分析过滤 ─────────────────────────────────────────────────

# 过滤阈值（可通过 config 覆盖）
DEFAULT_MIN_ELEMENTS = 4          # 元素数低于此值视为可疑
DEFAULT_MAX_TEXT_LEN = 2000       # 总文本低于此值视为广告
DEFAULT_MAX_REPEAT_RATIO = 0.80   # 文本重复率高于此值判定为封面
DEFAULT_MAX_TABLE_ELEMENTS = 5    # 表格页元素上限
DEFAULT_MIN_TABLE_TEXT_LEN = 5000 # 表格页文本下限
DEFAULT_MIN_LINE_REPEAT_RATIO = 0.50  # 表格页行重复率下限（表头重复比例）
DEFAULT_FRAG_AD_MIN_ELEMENTS = 6      # 碎片化广告：最少元素数
DEFAULT_FRAG_AD_SHORT_LEN = 100       # 碎片化广告：元素文字长度上限
DEFAULT_FRAG_AD_SHORT_RATIO = 0.80    # 碎片化广告：短元素占比下限
DEFAULT_CLASSIFIED_AD_MIN_CONTACTS = 3  # 分类广告：联系方式模式最少数量


def analyze_page_content(elements) -> Optional[Tuple[str, dict]]:
    """
    分析 OCR 输出特征，判断页面是否为非内容页（封面/广告）。

    基于实测数据建立的规则：
    - 封面特征：元素≤3 + 文本重复率>80%（封面标题被 VLM 拆分为多个相同文本块）
    - 广告特征：元素≤3 + 总文本≤200字符（纯广告页内容极少）

    Args:
        elements: OCR 元素列表（需有 .text 属性）

    Returns:
        None: 判定为内容页，保留
        (reason, metrics): 判定为非内容页，应过滤
    """
    if not elements:
        return None  # 空页不处理（可能是 OCR 失败）

    n_elements = len(elements)
    min_elements = config.get("page_filter.min_elements", DEFAULT_MIN_ELEMENTS)
    max_text_len = config.get("page_filter.max_text_len", DEFAULT_MAX_TEXT_LEN)
    max_repeat_ratio = config.get("page_filter.max_repeat_ratio", DEFAULT_MAX_REPEAT_RATIO)
    max_table_elements = config.get("page_filter.max_table_elements", DEFAULT_MAX_TABLE_ELEMENTS)
    min_table_text_len = config.get("page_filter.min_table_text_len", DEFAULT_MIN_TABLE_TEXT_LEN)
    min_line_repeat_ratio = config.get("page_filter.min_line_repeat_ratio", DEFAULT_MIN_LINE_REPEAT_RATIO)

    # 计算文本特征（所有规则共用）
    texts = [e.text.strip() for e in elements if e.text.strip()]
    total_text_len = sum(len(t) for t in texts)

    # 规则 3：碎片化广告检测 — 元素多但每个元素文字很短
    frag_min_elements = config.get("page_filter.frag_ad_min_elements", DEFAULT_FRAG_AD_MIN_ELEMENTS)
    frag_short_len = config.get("page_filter.frag_ad_short_len", DEFAULT_FRAG_AD_SHORT_LEN)
    frag_short_ratio = config.get("page_filter.frag_ad_short_ratio", DEFAULT_FRAG_AD_SHORT_RATIO)
    if n_elements >= frag_min_elements and texts:
        short_count = sum(1 for t in texts if len(t) < frag_short_len)
        actual_short_ratio = short_count / len(texts)
        if actual_short_ratio >= frag_short_ratio:
            from collections import Counter
            text_counts = Counter(texts)
            metrics = {
                "n_elements": n_elements,
                "total_text_len": total_text_len,
                "short_element_ratio": round(actual_short_ratio, 2),
                "short_element_threshold": frag_short_len,
            }
            reason = f"fragmented ad page ({n_elements} elements, {actual_short_ratio:.0%} texts < {frag_short_len} chars)"
            logger.info(f"碎片化广告检测命中: {reason}, metrics={metrics}")
            return (reason, metrics)

    # 规则 4：分类广告检测 — 多个联系方式模式（Tel/Email/www）
    import re
    classified_min_contacts = config.get("page_filter.classified_ad_min_contacts", DEFAULT_CLASSIFIED_AD_MIN_CONTACTS)
    contact_pattern = re.compile(r'(?:Tel:|Email:|www\.|https?://)', re.I)
    all_text_for_contacts = ' '.join(texts)
    contact_count = len(contact_pattern.findall(all_text_for_contacts))
    if contact_count >= classified_min_contacts:
        from collections import Counter
        text_counts = Counter(texts)
        metrics = {
            "n_elements": n_elements,
            "total_text_len": total_text_len,
            "contact_patterns": contact_count,
        }
        reason = f"classified ad page ({contact_count} contact patterns, {n_elements} elements)"
        logger.info(f"分类广告检测命中: {reason}, metrics={metrics}")
        return (reason, metrics)

    # 元素数充足 → 内容页
    if n_elements >= min_elements:
        return None

    if not texts:
        return None  # 无文本内容，不处理

    # 计算文本重复率：最常见文本的出现比例
    from collections import Counter
    text_counts = Counter(texts)
    most_common_count = text_counts.most_common(1)[0][1]
    repeat_ratio = most_common_count / len(texts) if len(texts) > 1 else 0.0

    metrics = {
        "n_elements": n_elements,
        "total_text_len": total_text_len,
        "unique_texts": len(text_counts),
        "repeat_ratio": round(repeat_ratio, 2),
    }

    # 规则 1：封面检测 — 元素少 + 文本高度重复
    if repeat_ratio >= max_repeat_ratio:
        reason = f"cover page (repeat ratio {repeat_ratio:.0%}, {n_elements} elements)"
        logger.info(f"封面检测命中: {reason}, metrics={metrics}")
        return (reason, metrics)

    # 规则 2：广告检测 — 元素少 + 文本短（两个条件同时满足）
    if total_text_len < max_text_len:
        reason = f"advertisement page (text {total_text_len} chars, {n_elements} elements)"
        logger.info(f"广告检测命中: {reason}, metrics={metrics}")
        return (reason, metrics)

    # 规则 3：纯表格页检测 — 元素极少 + 文本极长 + 行高度重复
    all_text = '\n'.join(texts)
    lines = [l.strip() for l in all_text.split('\n') if l.strip()]
    if lines:
        line_counts = Counter(lines)
        repeated_lines = sum(cnt for cnt in line_counts.values() if cnt > 1)
        line_repeat_ratio = repeated_lines / len(lines)
    else:
        line_repeat_ratio = 0.0
    metrics["line_repeat_ratio"] = round(line_repeat_ratio, 3)

    if n_elements < max_table_elements and total_text_len >= min_table_text_len and line_repeat_ratio >= min_line_repeat_ratio:
        reason = f"data table page (text {total_text_len} chars, {n_elements} elements, line repeat {line_repeat_ratio:.0%})"
        logger.info(f"表格页检测命中: {reason}, metrics={metrics}")
        return (reason, metrics)

    return None
