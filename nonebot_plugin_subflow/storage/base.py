"""StorageBackend 抽象接口。

业务层只依赖这个接口；具体后端（腾讯文档 / 飞书 / ...）实现它即可平替。
所有方法都是 async。所有 ID（file_id / sheet_id / record_id）类型都是字符串。
列值的 Python 类型与 FieldSchema.type 对应：
    text         -> str
    single_select-> str  (option 名)
    datetime     -> datetime.datetime
    number       -> int | float
    unknown      -> 原样 dict/list（不归一化）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import FieldSchema, Record


class StorageBackend(ABC):
    @abstractmethod
    async def get_fields(
        self, file_id: str, sheet_id: str, *, force_refresh: bool = False
    ) -> list[FieldSchema]:
        """读子表的列结构。结果应缓存在实例上（force_refresh=True 时跳过缓存）。"""

    @abstractmethod
    async def get_records(self, file_id: str, sheet_id: str) -> list[Record]:
        """读子表的所有记录（内部按需翻页）。"""

    @abstractmethod
    async def get_record(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> Record | None:
        """按 recordID 拉单条；找不到返回 None。"""

    @abstractmethod
    async def add_records(
        self, file_id: str, sheet_id: str, rows: list[dict[str, Any]]
    ) -> list[Record]:
        """插入若干条记录。rows 的 key 必须是表里已有列的标题。返回带 recordID 的结果。"""

    @abstractmethod
    async def update_record(
        self, file_id: str, sheet_id: str, record_id: str, values: dict[str, Any]
    ) -> Record:
        """更新单条记录的部分列。返回 API 给的响应（可能只含被更新的列）。

        D8：调用者关心完整最新值，可在此之后再调 get_record 重读。
        """

    @abstractmethod
    async def delete_records(
        self, file_id: str, sheet_id: str, record_ids: list[str]
    ) -> None:
        """删除若干条记录。"""

    async def aclose(self) -> None:
        """释放底层资源（如 http client）。默认实现什么都不做。"""
