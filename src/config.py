"""
配置加载模块
使用 PyYAML 加载 config.yaml，提供单例配置访问
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 默认配置文件路径
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 全局配置单例
_config: Optional[Dict[str, Any]] = None


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置文件
    
    Args:
        config_path: 配置文件路径，默认使用项目根目录下的 config.yaml
        
    Returns:
        配置字典
    """
    global _config
    
    if config_path is None:
        config_path = str(DEFAULT_CONFIG_PATH)
    
    config_file = Path(config_path)
    
    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_file}")
    
    with open(config_file, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    
    return _config


def get_config() -> Dict[str, Any]:
    """
    获取配置单例，如果未加载则自动加载默认配置
    
    Returns:
        配置字典
    """
    global _config
    
    if _config is None:
        load_config()
    
    return _config


def get(key: str, default: Any = None) -> Any:
    """
    获取配置项，支持点号分隔的嵌套键
    
    Args:
        key: 配置键，如 "ocr.api_base"
        default: 默认值
        
    Returns:
        配置值
    """
    config = get_config()
    keys = key.split(".")
    value = config
    
    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default
    
    return value


def get_path(key: str) -> Path:
    """
    获取路径配置项，返回绝对路径
    
    Args:
        key: 配置键，如 "paths.input_dir"
        
    Returns:
        绝对路径
    """
    relative_path = get(key)
    if relative_path is None:
        raise KeyError(f"路径配置不存在: {key}")
    
    path = Path(relative_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    
    return path


def update_config(updates: Dict[str, Any]) -> None:
    """
    更新配置项（用于命令行参数覆盖）
    
    Args:
        updates: 要更新的配置字典，支持点号分隔的嵌套键
    """
    config = get_config()
    
    for key, value in updates.items():
        keys = key.split(".")
        target = config
        
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        
        target[keys[-1]] = value
