"""子表记录的内存缓存 + 写穿透 + per-record 锁 + 30 分钟定时同步。

设计要点：
- 所有查询走缓存（零 API 消耗）
- 写穿透（D8）：storage 写成功后立即调 storage.get_record 重读，用最新值覆盖本地
- per-record asyncio.Lock（D5）：通过 lock_for() 暴露给上层；本模块的写方法默认 **不** 自动加锁，
  调用方需要在 lock_for 上下文内执行"读缓存 → 校验 → 写"序列，避免读后过时
- 定时同步：每 sync_interval_minutes 分钟全量拉一次（asyncio.Task 实现，start/stop 控制生命周期）

约定术语：
- "sheet ref"  = (file_id, sheet_id)
- "record key" = (file_id, sheet_id, record_id)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Iterable

from .exceptions import RecordNotFoundError
from .models import Record
from .storage.base import StorageBackend


log = logging.getLogger(__name__)


class SheetCache:
    def __init__(
        self,
        storage: StorageBackend,
        *,
        sync_interval_minutes: int = 30,
    ) -> None:
        self._storage = storage
        self._sync_interval_seconds = sync_interval_minutes * 60
        # {(file_id, sheet_id): {record_id: Record}}
        self._sheets: dict[tuple[str, str], dict[str, Record]] = {}
        # per-record locks（D5）
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._sync_task: asyncio.Task | None = None
        self._last_sync_at: datetime | None = None

    # ====================================================== sheet registration

    async def add_sheet(self, file_id: str, sheet_id: str) -> int:
        """注册一个子表并全量拉取。返回拉到的记录数。"""
        records = await self._storage.get_records(file_id, sheet_id)
        self._sheets[(file_id, sheet_id)] = {r.record_id: r for r in records}
        return len(records)

    def remove_sheet(self, file_id: str, sheet_id: str) -> None:
        """注销子表，丢掉缓存和相关锁。"""
        self._sheets.pop((file_id, sheet_id), None)
        for key in list(self._locks):
            if key[0] == file_id and key[1] == sheet_id:
                self._locks.pop(key, None)

    def list_sheets(self) -> list[tuple[str, str]]:
        return list(self._sheets.keys())

    @property
    def last_sync_at(self) -> datetime | None:
        return self._last_sync_at

    # ====================================================== read (zero API)

    def get_record(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> Record | None:
        return self._sheets.get((file_id, sheet_id), {}).get(record_id)

    def get_records(self, file_id: str, sheet_id: str) -> list[Record]:
        return list(self._sheets.get((file_id, sheet_id), {}).values())

    def find_records(
        self,
        file_id: str,
        sheet_id: str,
        predicate,
    ) -> list[Record]:
        return [r for r in self.get_records(file_id, sheet_id) if predicate(r)]

    # ====================================================== locks (D5)

    def lock_for(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> asyncio.Lock:
        """获取某条 record 的写锁。上层 task_manager 应在锁内做"读缓存→校验→写"序列。"""
        key = (file_id, sheet_id, record_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ====================================================== writes (call inside lock_for)

    async def add_records(
        self, file_id: str, sheet_id: str, rows: list[dict[str, Any]]
    ) -> list[Record]:
        """插入。新 record 还不存在，无需 per-record 锁。"""
        inserted = await self._storage.add_records(file_id, sheet_id, rows)
        sheet = self._sheets.setdefault((file_id, sheet_id), {})
        for rec in inserted:
            sheet[rec.record_id] = rec
        return inserted

    async def update_record(
        self,
        file_id: str,
        sheet_id: str,
        record_id: str,
        values: dict[str, Any],
    ) -> Record:
        """更新 + D8 写后重读。调用方应已持有 lock_for(...) 锁。"""
        await self._storage.update_record(file_id, sheet_id, record_id, values)
        latest = await self._storage.get_record(file_id, sheet_id, record_id)
        sheet = self._sheets.setdefault((file_id, sheet_id), {})
        if latest is None:
            sheet.pop(record_id, None)
            raise RecordNotFoundError(
                f"record {record_id} disappeared after update"
            )
        sheet[record_id] = latest
        return latest

    async def delete_records(
        self, file_id: str, sheet_id: str, record_ids: list[str]
    ) -> None:
        """批量删除。"""
        await self._storage.delete_records(file_id, sheet_id, record_ids)
        sheet = self._sheets.get((file_id, sheet_id), {})
        for rid in record_ids:
            sheet.pop(rid, None)
            self._locks.pop((file_id, sheet_id, rid), None)

    # ====================================================== refresh

    async def refresh_record(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> Record | None:
        """重新拉单条 record；远端不存在则从缓存移除。"""
        rec = await self._storage.get_record(file_id, sheet_id, record_id)
        sheet = self._sheets.setdefault((file_id, sheet_id), {})
        if rec is None:
            sheet.pop(record_id, None)
        else:
            sheet[record_id] = rec
        return rec

    async def refresh_sheet(self, file_id: str, sheet_id: str) -> int:
        """重新全量拉单个子表。返回拉到的记录数。"""
        records = await self._storage.get_records(file_id, sheet_id)
        self._sheets[(file_id, sheet_id)] = {r.record_id: r for r in records}
        return len(records)

    async def refresh_all(self) -> dict[tuple[str, str], int | Exception]:
        """全量同步所有已注册子表。返回 {sheet_ref: 记录数 | 异常}。"""
        results: dict[tuple[str, str], int | Exception] = {}
        for sheet_ref in self.list_sheets():
            try:
                results[sheet_ref] = await self.refresh_sheet(*sheet_ref)
            except Exception as exc:  # 单个表失败不影响其他表
                log.exception("refresh_sheet failed for %s: %s", sheet_ref, exc)
                results[sheet_ref] = exc
        self._last_sync_at = datetime.now()
        return results

    # ====================================================== periodic sync task

    async def start(self) -> None:
        """启动 30 分钟定时同步任务。重复启动是空操作。"""
        if self._sync_task is not None and not self._sync_task.done():
            return
        self._sync_task = asyncio.create_task(
            self._sync_loop(), name="subflow-cache-sync"
        )

    async def stop(self) -> None:
        """停止定时同步任务。"""
        task = self._sync_task
        self._sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _sync_loop(self) -> None:
        log.info("subflow cache sync started (interval=%ss)", self._sync_interval_seconds)
        try:
            while True:
                await asyncio.sleep(self._sync_interval_seconds)
                try:
                    results = await self.refresh_all()
                    log.info(
                        "periodic sync done at %s: %s",
                        self._last_sync_at,
                        {k: v for k, v in results.items()},
                    )
                except Exception:  # noqa: BLE001
                    log.exception("periodic sync iteration failed")
        except asyncio.CancelledError:
            log.info("subflow cache sync stopped")
            raise
