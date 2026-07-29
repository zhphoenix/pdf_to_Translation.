"""
事件驱动通知模块

程序运行状态变更时主动写入信号文件，替代 IDE 轮询模式。
IDE 可通过监听信号文件实现即时感知。

信号文件: output/.pipeline_status.json
信号格式:
{
    "event": "step_start" | "step_done" | "step_error" | "pipeline_done" | "pipeline_error",
    "step": "ocr" | "translate" | "full",
    "timestamp": 1234567890.123,
    "pid": 12345,
    "data": { ... }  // 事件相关数据
}
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any

from .utils import get_logger

# 信号文件名（固定路径，IDE 监听此文件）
STATUS_FILENAME = ".pipeline_status.json"


@dataclass
class PipelineEvent:
    """流水线事件"""
    event: str                    # 事件类型
    step: str                     # 步骤名称
    timestamp: float = 0.0        # 时间戳
    pid: int = 0                  # 进程 ID
    data: Dict[str, Any] = field(default_factory=dict)  # 附加数据

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        if self.pid == 0:
            self.pid = os.getpid()


class Notifier:
    """
    流水线状态通知器

    用法:
        notifier = Notifier(output_dir)
        notifier.notify_step_start("ocr", total_pages=84)
        notifier.notify_step_done("ocr", elapsed=299.7, output="output/xxx.ocr.json")
        notifier.notify_pipeline_done(total_files=1, output_dir="output/")
    """

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.status_file = self.output_dir / STATUS_FILENAME
        self.logger = get_logger()

    def _write_event(self, event: PipelineEvent):
        """写入事件到信号文件（原子写入）"""
        tmp_file = self.status_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(asdict(event), f, ensure_ascii=False, indent=2)
            # 原子重命名（同文件系统）
            tmp_file.replace(self.status_file)
            self.logger.debug(f"通知: {event.event} ({event.step})")
        except Exception as e:
            self.logger.warning(f"通知写入失败: {e}")

    def notify_step_start(self, step: str, **kwargs):
        """通知步骤开始"""
        event = PipelineEvent(
            event="step_start",
            step=step,
            data=kwargs
        )
        self._write_event(event)

    def notify_step_progress(self, step: str, current: int, total: int, **kwargs):
        """通知步骤进度"""
        event = PipelineEvent(
            event="step_progress",
            step=step,
            data={"current": current, "total": total, **kwargs}
        )
        self._write_event(event)

    def notify_step_done(self, step: str, **kwargs):
        """通知步骤完成"""
        event = PipelineEvent(
            event="step_done",
            step=step,
            data=kwargs
        )
        self._write_event(event)

    def notify_step_error(self, step: str, error: str, **kwargs):
        """通知步骤出错"""
        event = PipelineEvent(
            event="step_error",
            step=step,
            data={"error": error, **kwargs}
        )
        self._write_event(event)

    def notify_pipeline_done(self, **kwargs):
        """通知整个流水线完成"""
        event = PipelineEvent(
            event="pipeline_done",
            step="full",
            data=kwargs
        )
        self._write_event(event)
        self.logger.info(f"✓ 流水线完成，信号已发送: {self.status_file}")

    def notify_pipeline_error(self, error: str, **kwargs):
        """通知整个流水线出错"""
        event = PipelineEvent(
            event="pipeline_error",
            step="full",
            data={"error": error, **kwargs}
        )
        self._write_event(event)
        self.logger.error(f"✗ 流水线出错，信号已发送: {self.status_file}")


def read_status(output_dir: Path) -> Optional[PipelineEvent]:
    """
    读取当前状态信号文件（供外部工具调用）

    Args:
        output_dir: 输出目录

    Returns:
        PipelineEvent 或 None
    """
    status_file = Path(output_dir) / STATUS_FILENAME
    if not status_file.exists():
        return None
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return PipelineEvent(**data)
    except Exception:
        return None
