"""
OCR 模块
OCRElement 数据结构 + 引擎工厂
"""

from dataclasses import dataclass
from typing import Tuple


@dataclass
class OCRElement:
    """OCR 结构化元素"""
    type: str                         # title, heading, text, list_item, caption, quote
    bbox: Tuple[int, int, int, int]   # (x1, y1, x2, y2)
    text: str                         # 文本内容
    page: int = 0                     # 所属页码

    @property
    def x1(self) -> int:
        return self.bbox[0]

    @property
    def y1(self) -> int:
        return self.bbox[1]

    @property
    def x2(self) -> int:
        return self.bbox[2]

    @property
    def y2(self) -> int:
        return self.bbox[3]

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def to_dict(self) -> dict:
        """转为 JSON 可序列化字典"""
        return {
            "type": self.type,
            "bbox": list(self.bbox),
            "text": self.text,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OCRElement":
        """从字典反序列化"""
        return cls(
            type=d["type"],
            bbox=tuple(d["bbox"]),
            text=d["text"],
            page=d.get("page", 0)
        )


def create_ocr_engine():
    """
    创建 OCR 引擎实例（PaddleOCR-VL VLM）

    Returns:
        PaddleOCREngine 实例
    """
    from .paddleocr_engine import create_paddleocr_engine
    return create_paddleocr_engine()
"""
OCR 模块
调用 Unlimited-OCR vLLM API（OpenAI Compatible API）进行图片文字识别
输出结构化 OCR 结果（类型 + 坐标 + 文本）

vLLM 部署要求:
- Prompt 必须以 <image> 开头
- skip_special_tokens=False（保留 <|det|> 标记）
- 需传入 vllm_xargs: ngram_size / window_size
"""

import base64
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from openai import OpenAI

from . import config
from .utils import get_logger, encode_image_to_base64, get_image_mime_type

logger = get_logger()

# ─── OCR Prompt（vLLM 要求 <image> 前缀）───────────────────────────
OCR_PROMPT_SINGLE = "<image>document parsing."
OCR_PROMPT_MULTI = "<image>Multi page parsing."

# 默认使用单页模式
OCR_PROMPT = OCR_PROMPT_SINGLE


# ─── 数据结构 ──────────────────────────────────────────────────────
@dataclass
class OCRElement:
    """OCR 结构化元素"""
    type: str                         # title, text, header, footer, page_number, caption, list_item, table_cell
    bbox: Tuple[int, int, int, int]   # (x1, y1, x2, y2)
    text: str                         # 文本内容
    page: int = 0                     # 所属页码

    @property
    def x1(self) -> int:
        return self.bbox[0]

    @property
    def y1(self) -> int:
        return self.bbox[1]

    @property
    def x2(self) -> int:
        return self.bbox[2]

    @property
    def y2(self) -> int:
        return self.bbox[3]

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def to_dict(self) -> dict:
        """转为 JSON 可序列化字典"""
        return {
            "type": self.type,
            "bbox": list(self.bbox),
            "text": self.text,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OCRElement":
        """从字典反序列化"""
        return cls(
            type=d["type"],
            bbox=tuple(d["bbox"]),
            text=d["text"],
            page=d.get("page", 0)
        )


# ─── 解析正则 ──────────────────────────────────────────────────────
# vLLM 输出格式: <|det|>type [x1, y1, x2, y2]<|/det|>content
VLLM_DET_PATTERN = re.compile(
    r'<\|det\|>\s*(\w+)\s*\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\s*<\|/det\|>\s*(.*)'
)

# 兼容旧 llama.cpp 格式: type [x1, y1, x2, y2]content（回退用）
LEGACY_PATTERN = re.compile(
    r'^(\w+)\s+\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\s*(.*)$'
)

# 需要过滤的特殊标记行
_SKIP_MARKERS = ('<|ref|>', '<|/ref|>', '```')


def parse_ocr_output(raw: str, page_num: int = 0) -> List[OCRElement]:
    """
    解析 OCR 结构化输出（支持 vLLM 和 llama.cpp 两种格式）

    vLLM 格式:
        <|det|>title [37, 64, 464, 132]<|/det|>INVOICE #2026-0623
        <|det|>text [37, 194, 350, 247]<|/det|>Bill To: Sahil Chachra

    llama.cpp 旧格式（兼容）:
        header [53, 30, 230, 43]The Economist May 30th 2026
        text [50, 95, 339, 239]This approach still causes problems...

    兜底策略:
        1. 优先 VLLM_DET_PATTERN
        2. 回退 LEGACY_PATTERN
        3. 仍失败 → 非空行保留为 type="text", bbox=(0,0,0,0)
        4. 跳过特殊标记行

    Args:
        raw: OCR 原始输出文本
        page_num: 页码

    Returns:
        OCRElement 列表
    """
    elements = []

    for line in raw.split('\n'):
        line = line.strip()
        if not line:
            continue

        # 跳过特殊标记
        if any(marker in line for marker in _SKIP_MARKERS):
            continue

        # 1. 尝试 vLLM 格式
        match = VLLM_DET_PATTERN.match(line)
        if match:
            elem_type = match.group(1).lower()
            bbox = (
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5))
            )
            text = match.group(6).strip()
            if text:
                elements.append(OCRElement(
                    type=elem_type, bbox=bbox, text=text, page=page_num
                ))
            continue

        # 2. 回退 llama.cpp 旧格式
        match = LEGACY_PATTERN.match(line)
        if match:
            elem_type = match.group(1).lower()
            bbox = (
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5))
            )
            text = match.group(6).strip()
            if text:
                elements.append(OCRElement(
                    type=elem_type, bbox=bbox, text=text, page=page_num
                ))
            continue

        # 3. 兜底：非空行作为纯文本保留
        # 过滤残余特殊 token
        cleaned = re.sub(r'<\|[^|]*\|>', '', line).strip()
        if cleaned:
            elements.append(OCRElement(
                type="text", bbox=(0, 0, 0, 0), text=cleaned, page=page_num
            ))

    return elements


# ─── OCR 引擎 ──────────────────────────────────────────────────────
class OCREngine:
    """OCR 引擎类，封装 Unlimited-OCR vLLM API 调用"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None
    ):
        """
        初始化 OCR 引擎

        Args:
            api_base: API 基础 URL
            model: 模型名称（对应 --served-model-name）
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
        """
        self.api_base = api_base or config.get("ocr.api_base", "http://localhost:8000/v1")
        self.model = model or config.get("ocr.model", "unlimited-ocr")
        self.timeout = timeout or config.get("ocr.timeout", 300)
        self.max_retries = max_retries or config.get("ocr.max_retries", 3)

        # vLLM 专用参数
        self.skip_special_tokens = config.get("ocr.skip_special_tokens", False)
        self.ngram_size = config.get("ocr.ngram_size", 15)
        self.ngram_window_single = config.get("ocr.ngram_window_single", 128)
        self.ngram_window_multi = config.get("ocr.ngram_window_multi", 1024)
        self.repetition_penalty = config.get("ocr.repetition_penalty", 1.1)
        self.temperature = config.get("ocr.temperature", 0.1)

        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            base_url=self.api_base,
            api_key="EMPTY",  # vLLM 不需要真实 API key
            timeout=self.timeout
        )

        logger.info(
            f"OCR 引擎初始化: {self.api_base}, 模型: {self.model}, "
            f"temp={self.temperature}, rep_penalty={self.repetition_penalty}, "
            f"ngram_size={self.ngram_size}"
        )

    def ocr_image(self, image_path: Path, page_num: int = 0) -> List[OCRElement]:
        """
        对单张图片进行 OCR，返回结构化元素

        Args:
            image_path: 图片文件路径
            page_num: 页码

        Returns:
            OCRElement 列表
        """
        base64_image = encode_image_to_base64(image_path)
        mime_type = get_image_mime_type(image_path)
        raw = self._call_api(base64_image, mime_type)
        return parse_ocr_output(raw, page_num)

    def ocr_image_bytes(
        self, image_bytes: bytes, mime_type: str = None, page_num: int = 0
    ) -> List[OCRElement]:
        """
        对图片字节数据进行 OCR

        Args:
            image_bytes: 图片字节数据
            mime_type: 图片 MIME 类型（默认自动检测 JPEG/PNG）
            page_num: 页码

        Returns:
            OCRElement 列表
        """
        # 自动检测图片格式
        if mime_type is None:
            if image_bytes[:2] == b'\xff\xd8':
                mime_type = "image/jpeg"
            else:
                mime_type = "image/png"

        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        raw = self._call_api(base64_image, mime_type)
        elements = parse_ocr_output(raw, page_num)

        # 兜底：整页解析为空 → 重试一次（换用 MULTI prompt）
        if not elements and raw.strip():
            logger.warning(f"第 {page_num} 页解析为空，尝试 MULTI prompt 重试...")
            raw_retry = self._call_api(base64_image, mime_type, prompt=OCR_PROMPT_MULTI)
            elements = parse_ocr_output(raw_retry, page_num)
            if not elements:
                logger.warning(f"第 {page_num} 页重试仍为空，标记为空页")

        return elements

    def ocr_image_raw(self, image_path: Path) -> str:
        """
        对单张图片进行 OCR，返回原始文本（调试用）

        Args:
            image_path: 图片文件路径

        Returns:
            OCR 原始输出
        """
        base64_image = encode_image_to_base64(image_path)
        mime_type = get_image_mime_type(image_path)
        return self._call_api(base64_image, mime_type)

    def _call_api(
        self, base64_image: str, mime_type: str, prompt: Optional[str] = None
    ) -> str:
        """
        调用 vLLM OCR API

        Args:
            base64_image: base64 编码的图片
            mime_type: 图片 MIME 类型
            prompt: 自定义 prompt（默认使用 OCR_PROMPT_SINGLE）

        Returns:
            OCR 原始输出文本
        """
        if prompt is None:
            prompt = OCR_PROMPT

        data_url = f"data:{mime_type};base64,{base64_image}"

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    }
                ]
            }
        ]

        # 确定 ngram window（单页 vs 多页）
        window_size = self.ngram_window_single
        if prompt == OCR_PROMPT_MULTI:
            window_size = self.ngram_window_multi

        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=16384,
                    temperature=self.temperature,
                    extra_body={
                        "skip_special_tokens": self.skip_special_tokens,
                        "repetition_penalty": self.repetition_penalty,
                        "vllm_xargs": {
                            "ngram_size": self.ngram_size,
                            "window_size": window_size,
                        },
                    }
                )

                result = response.choices[0].message.content
                return result.strip() if result else ""

            except Exception as e:
                logger.warning(f"OCR 请求失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error("OCR 请求最终失败")
                    raise


def create_ocr_engine():
    """
    创建 OCR 引擎实例

    根据配置 ocr.engine 选择引擎:
    - "unlimited-ocr": Unlimited-OCR vLLM API (默认)
    - "paddleocr": PaddleOCR-VL 全流水线服务

    Returns:
        OCR 引擎实例（OCREngine 或 PaddleOCREngine）
    """
    engine_type = config.get("ocr.engine", "unlimited-ocr")

    if engine_type == "paddleocr":
        from .paddleocr_engine import create_paddleocr_engine
        return create_paddleocr_engine()
    else:
        return OCREngine()
