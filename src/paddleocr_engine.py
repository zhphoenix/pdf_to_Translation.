"""
PaddleOCR-VL VLM 引擎模块
直接调用 VLM 推理服务（端口 8118）提取文档中的文章文本
输出: 纯文本段落列表（OCRElement），供下游翻译/输出
"""

import base64
import re
import time
from pathlib import Path
from typing import List, Optional

from openai import OpenAI

from . import config
from .ocr import OCRElement
from .utils import get_logger, encode_image_to_base64, get_image_mime_type

logger = get_logger()

# ─── Prompt ──────────────────────────────────────────────────────
# 简洁直接：只要文章正文，不要表格/页眉页脚/广告
OCR_SYSTEM_PROMPT = (
    "You are a document text extractor. "
    "Extract ALL article text from this image in reading order.\n"
    "Output format (strict Markdown):\n"
    "- Use # for the main document title (magazine name, issue date)\n"
    "- Use ## for section headings (article titles, column names)\n"
    "- Use ### for sub-headings within articles\n"
    "- Separate paragraphs with blank lines\n"
    "- Use - for bullet list items\n\n"
    "Rules:\n"
    "- Skip tables, charts, images, advertisements\n"
    "- Skip page numbers, headers, footers, copyright notices\n"
    "- Skip subscription prompts and ads\n"
    "- Do NOT add any commentary, description, or summary\n"
    "- Do NOT modify or summarize the original text\n"
    "- Output text EXACTLY as it appears in the image"
)

OCR_USER_PROMPT = "Extract the article text from this image as Markdown."


# ─── VLM 输出 → OCRElement ──────────────────────────────────────
def parse_vlm_output(markdown_text: str, page_num: int = 0) -> List[OCRElement]:
    """
    将 VLM 输出的 Markdown 文本拆分为 OCRElement 列表
    只保留: 标题(title/heading) + 正文段落(text) + 列表(list_item)
    丢弃: 表格、引用、代码块、图片占位符
    """
    elements = []
    if not markdown_text or not markdown_text.strip():
        return elements

    # 按双换行分割块
    blocks = re.split(r'\n{2,}', markdown_text.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # 跳过代码块
        if block.startswith('```'):
            continue

        # 跳过图片占位符
        if re.match(r'^!\[.*\]\(.*\)$', block):
            continue

        # 跳过纯表格块（全由 | 开头的行组成）
        lines = block.split('\n')
        table_lines = [l for l in lines if l.strip().startswith('|')]
        if len(table_lines) == len(lines):
            continue

        # 跳过分隔线
        if re.match(r'^[-=_*]{3,}\s*$', block):
            continue

        # 分类并提取
        if block.startswith('# ') and not block.startswith('## '):
            # 一级标题
            text = block[2:].strip()
            if text:
                elements.append(OCRElement(
                    type="title", bbox=(0, 0, 0, 0), text=text, page=page_num
                ))

        elif block.startswith('## '):
            # 二级及以下标题
            text = re.sub(r'^#+\s*', '', block).strip()
            if text:
                elements.append(OCRElement(
                    type="heading", bbox=(0, 0, 0, 0), text=text, page=page_num
                ))

        elif block.startswith('### ') or block.startswith('#### '):
            text = re.sub(r'^#+\s*', '', block).strip()
            if text:
                elements.append(OCRElement(
                    type="heading", bbox=(0, 0, 0, 0), text=text, page=page_num
                ))

        elif block.startswith('- ') or block.startswith('* ') or re.match(r'^\d+[\.\)]\s', block):
            # 列表项 — 每行一个元素
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                text = re.sub(r'^[-*]\s+', '', line)
                text = re.sub(r'^\d+[\.\)]\s+', '', text)
                if text:
                    elements.append(OCRElement(
                        type="list_item", bbox=(0, 0, 0, 0), text=text, page=page_num
                    ))

        elif block.startswith('> '):
            # 引用 — 跳过（通常是图注/广告）
            continue

        else:
            # 普通段落 — 清理内联 Markdown 格式
            text = block.strip()
            # 移除加粗/斜体标记
            text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
            text = re.sub(r'\*(.+?)\*', r'\1', text)
            text = re.sub(r'__(.+?)__', r'\1', text)
            text = re.sub(r'_(.+?)_', r'\1', text)
            # 移除行内链接 [text](url) → text
            text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
            # 移除行内代码标记
            text = text.replace('`', '')

            if text and len(text) > 2:
                elements.append(OCRElement(
                    type="text", bbox=(0, 0, 0, 0), text=text, page=page_num
                ))

    return elements


# ─── VLM 幻觉/垃圾内容检测 ─────────────────────────────────────
# VLM 可能输出的垃圾前缀（系统提示泄漏、模型描述等）
_HALLUCINATION_PREFIXES = (
    "The image shows",
    "The image contains",
    "This image displays",
    "In this image",
    "The picture shows",
    "Here is the extracted",
    "Here are the extracted",
    "Below is the text",
    "The following is",
    "The Ground Truth",
    "According to",
    "The best answer",
)

_HALLUCINATION_PATTERNS = [
    re.compile(r'^t2t2[,0]*$'),
    re.compile(r't2t2[,\d]{10,}'),
    re.compile(r'^\|?\|+$'),
    re.compile(r'^(\S+)(\s+\1){5,}\s*$'),          # 同一 token 重复 6+ 次
    re.compile(r'(\[Non-Text\]\s*){3,}', re.I),
    re.compile(r'^\[No text detected\]\s*$', re.I),
    re.compile(r'^#{1,6}\s*t2t2'),
]


def _is_garbage(text: str) -> bool:
    """检测 VLM 输出的垃圾内容"""
    if not text:
        return True
    # 前缀匹配
    for prefix in _HALLUCINATION_PREFIXES:
        if text.startswith(prefix):
            return True
    # 模式匹配
    for pat in _HALLUCINATION_PATTERNS:
        if pat.search(text):
            return True
    return False


# ─── 引擎类 ─────────────────────────────────────────────────────
class PaddleOCREngine:
    """PaddleOCR-VL VLM 引擎"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        self.api_base = api_base or config.get("paddleocr.api_base", "http://localhost:8118/v1")
        self.model = model or config.get("paddleocr.model", "PaddleOCR-VL-1.6-0.9B")
        self.timeout = timeout or config.get("paddleocr.timeout", 300)
        self.max_retries = max_retries or config.get("paddleocr.max_retries", 3)
        self.temperature = config.get("paddleocr.temperature", 0.0)
        self.max_tokens = config.get("paddleocr.max_tokens", 8192)

        self.client = OpenAI(
            base_url=self.api_base,
            api_key="EMPTY",
            timeout=self.timeout,
        )

        logger.info(
            f"PaddleOCR 引擎: {self.api_base}, 模型={self.model}, "
            f"temp={self.temperature}, max_tokens={self.max_tokens}"
        )

    def ocr_image(self, image_path: Path, page_num: int = 0) -> List[OCRElement]:
        """对单张图片 OCR → OCRElement 列表"""
        b64 = encode_image_to_base64(image_path)
        mime = get_image_mime_type(image_path)
        raw = self._call_vlm(b64, mime)
        return self._parse_and_clean(raw, page_num)

    def ocr_image_bytes(
        self, image_bytes: bytes, mime_type: str = None, page_num: int = 0
    ) -> List[OCRElement]:
        """对图片字节数据 OCR → OCRElement 列表"""
        if mime_type is None:
            mime_type = "image/jpeg" if image_bytes[:2] == b'\xff\xd8' else "image/png"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        raw = self._call_vlm(b64, mime_type)
        elements = self._parse_and_clean(raw, page_num)

        # 空结果重试一次
        if not elements and raw.strip():
            logger.warning(f"Page {page_num}: 解析为空，重试...")
            raw2 = self._call_vlm(b64, mime_type)
            elements = self._parse_and_clean(raw2, page_num)

        return elements

    def ocr_image_raw(self, image_path: Path) -> str:
        """返回 VLM 原始输出（调试用）"""
        b64 = encode_image_to_base64(image_path)
        mime = get_image_mime_type(image_path)
        return self._call_vlm(b64, mime)

    def health_check(self) -> bool:
        """检查 VLM 服务是否可用"""
        try:
            import requests
            base = self.api_base.rstrip('/').rsplit('/v1', 1)[0]
            return requests.get(f"{base}/health", timeout=10).status_code == 200
        except Exception:
            return False

    # ─── 内部方法 ────────────────────────────────────────────

    def _parse_and_clean(self, raw: str, page_num: int) -> List[OCRElement]:
        """解析 VLM 输出 + 垃圾过滤"""
        elements = parse_vlm_output(raw, page_num)
        # 过滤垃圾内容
        return [e for e in elements if not _is_garbage(e.text)]

    def _call_vlm(self, b64_image: str, mime_type: str) -> str:
        """调用 VLM API"""
        data_url = f"data:{mime_type};base64,{b64_image}"
        messages = [
            {"role": "system", "content": OCR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": OCR_USER_PROMPT},
                ],
            },
        ]

        for attempt in range(self.max_retries):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
                result = resp.choices[0].message.content
                return result.strip() if result else ""
            except Exception as e:
                logger.warning(
                    f"VLM 请求失败 ({attempt + 1}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


def create_paddleocr_engine() -> PaddleOCREngine:
    """创建 PaddleOCR 引擎实例"""
    return PaddleOCREngine()
