"""
工具函数模块
提供日志配置、文件路径处理、进度显示、图片编码等功能
"""

import base64
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from . import config


class TqdmLoggingHandler(logging.StreamHandler):
    """
    兼容 tqdm 的日志 Handler
    通过 tqdm.write() 输出日志，避免与进度条的 \r 刷新冲突。
    日志显示在进度条上方，进度条始终保持在最底行。
    """

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stdout)
        except Exception:
            self.handleError(record)


def setup_logger(name: str = "paddleocr", level: int = logging.INFO) -> logging.Logger:
    """
    配置日志记录器，同时输出到终端和日志文件
    终端输出使用 TqdmLoggingHandler，与进度条共存不冲突
    
    Args:
        name: 日志记录器名称
        level: 日志级别
        
    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    if logger.handlers:
        return logger
    
    logger.setLevel(level)
    
    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 终端输出（通过 tqdm.write，与进度条共存）
    console_handler = TqdmLoggingHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出
    log_dir = config.get_path("paths.log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"ocr_{timestamp}.log"
    
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger


def get_logger() -> logging.Logger:
    """获取全局日志记录器"""
    return logging.getLogger("paddleocr")


def encode_image_to_base64(image_path: Path) -> str:
    """
    将图片文件编码为 base64 字符串
    
    Args:
        image_path: 图片文件路径
        
    Returns:
        base64 编码的字符串
    """
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_image_mime_type(image_path: Path) -> str:
    """
    获取图片的 MIME 类型
    
    Args:
        image_path: 图片文件路径
        
    Returns:
        MIME 类型字符串
    """
    suffix = image_path.suffix.lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    return mime_types.get(suffix, "image/png")


def create_progress_bar(total: int, desc: str = "处理中") -> tqdm:
    """
    创建进度条（兼容 IDE 内置终端和外部终端）
    
    - 输出到 stdout（IDE 终端可见）
    - dynamic_ncols 自适应终端宽度
    - position=0, leave=True 确保进度条固定显示
    
    Args:
        total: 总数
        desc: 描述文字
        
    Returns:
        tqdm 进度条对象
    """
    return tqdm(
        total=total,
        desc=desc,
        unit="页",
        file=sys.stdout,
        position=0,
        leave=True,
        dynamic_ncols=True,    # 自适应终端宽度
        mininterval=1,         # 最少 1 秒刷新
        ascii=" =#",           # ASCII 进度字符（兼容所有终端）
    )


def ensure_dir(path: Path) -> Path:
    """
    确保目录存在，不存在则创建
    
    Args:
        path: 目录路径
        
    Returns:
        目录路径
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_pdf_files(input_path: Path) -> list:
    """
    获取 PDF 文件列表
    
    Args:
        input_path: 输入路径（文件或目录）
        
    Returns:
        PDF 文件路径列表
    """
    if input_path.is_file():
        if input_path.suffix.lower() == ".pdf":
            return [input_path]
        else:
            raise ValueError(f"不是 PDF 文件: {input_path}")
    
    if input_path.is_dir():
        pdf_files = sorted(input_path.glob("*.pdf"))
        if not pdf_files:
            raise ValueError(f"目录中没有 PDF 文件: {input_path}")
        return pdf_files
    
    raise FileNotFoundError(f"路径不存在: {input_path}")


def sanitize_filename(filename: str) -> str:
    """
    清理文件名，移除非法字符
    
    Args:
        filename: 原始文件名
        
    Returns:
        清理后的文件名
    """
    # 移除路径分隔符和非法字符
    illegal_chars = '<>:"/\\|?*'
    for char in illegal_chars:
        filename = filename.replace(char, "_")
    return filename


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小
    
    Args:
        size_bytes: 字节数
        
    Returns:
        格式化后的字符串
    """
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
