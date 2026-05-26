"""Storage 抽象层 + 各后端实现。

外部代码用 `from nonebot_plugin_subflow.storage import StorageBackend, TencentDocStorage`。
"""

from .base import StorageBackend
from .tencent_doc import TencentDocStorage

__all__ = ["StorageBackend", "TencentDocStorage"]
