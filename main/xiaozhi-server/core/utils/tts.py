import os
import re
import sys
import importlib

from config.logger import setup_logging
from core.utils.text_utils import remove_emojis
from core.utils.tts_text import MarkdownCleaner, punctuation_set

logger = setup_logging()

def create_instance(class_name, *args, **kwargs):
    # 创建TTS实例
    if os.path.exists(os.path.join('core', 'providers', 'tts', f'{class_name}.py')):
        lib_name = f'core.providers.tts.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].TTSProvider(*args, **kwargs)

    raise ValueError(f"不支持的TTS类型: {class_name}，请检查该配置的type是否设置正确")


def convert_percentage_to_range(percentage, min_val, max_val, base_val=None):
    """
    将百分比(-100~100)转换为指定范围的值

    Args:
        percentage: 百分比值 (-100 到 100)
        min_val: 目标范围最小值
        max_val: 目标范围最大值
        base_val: 基准值（可选，默认为范围中点）

    Returns:
        转换后的值
    """
    percentage, min_val, max_val = float(percentage), float(min_val), float(max_val)
    base_val = float(base_val) if base_val is not None else (min_val + max_val) / 2

    if percentage < 0:
        # 负百分比：从 base_val 向 min_val 线性插值
        result = base_val + (base_val - min_val) * (percentage / 100)
    else:
        # 正百分比：从 base_val 向 max_val 线性插值
        result = base_val + (max_val - base_val) * (percentage / 100)

    # 确保结果在有效范围内
    return max(min_val, min(max_val, result))
