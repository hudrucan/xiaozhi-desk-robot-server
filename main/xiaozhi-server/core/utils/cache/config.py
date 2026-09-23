"""
缓存配置管理
"""

from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass
from .strategies import CacheStrategy


class CacheType(Enum):
    """缓存类型枚举"""

    LOCATION = "location"
    WEATHER = "weather"
    INTENT = "intent"
    CONFIG = "config"
    DEVICE_PROMPT = "device_prompt"
    VOICEPRINT_HEALTH = "voiceprint_health"  # 声纹识别健康检查
    AUDIO_DATA = "audio_data"  # 音频数据缓存


@dataclass
class CacheConfig:
    """缓存配置类"""

    strategy: CacheStrategy = CacheStrategy.TTL
    ttl: Optional[float] = 300  # 默认5分钟
    max_size: Optional[int] = 1000  # 默认最大1000条
    cleanup_interval: float = 60  # 清理间隔（秒）

    @classmethod
    def for_type(cls, cache_type: CacheType) -> "CacheConfig":
        """根据缓存类型返回预设配置"""
        configs = {
            CacheType.LOCATION: cls(
                strategy=CacheStrategy.TTL, ttl=None, max_size=1000  # 手动失效
            ),
            CacheType.WEATHER: cls(
                strategy=CacheStrategy.TTL, ttl=28800, max_size=1000  # 8小时
            ),
            CacheType.INTENT: cls(
                strategy=CacheStrategy.TTL_LRU, ttl=600, max_size=1000  # 10分钟
            ),
            CacheType.CONFIG: cls(
                strategy=CacheStrategy.FIXED_SIZE, ttl=None, max_size=20  # 手动失效
            ),
            CacheType.DEVICE_PROMPT: cls(
                strategy=CacheStrategy.TTL, ttl=None, max_size=1000  # 手动失效
            ),
            CacheType.VOICEPRINT_HEALTH: cls(
                strategy=CacheStrategy.TTL, ttl=600, max_size=100  # 10分钟过期
            ),
            CacheType.AUDIO_DATA: cls(
                strategy=CacheStrategy.TTL, ttl=600, max_size=100  # 10分钟过期
            ),
        }
        return configs.get(cache_type, cls())
