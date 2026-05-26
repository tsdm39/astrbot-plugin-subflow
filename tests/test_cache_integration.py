"""cache.py 与真实腾讯文档 API 的端到端集成测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from nonebot_plugin_subflow.cache import SheetCache
from nonebot_plugin_subflow.storage import TencentDocStorage


pytestmark = pytest.mark.integration


async def test_full_cycle_via_cache(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    """add_sheet → add_records → update_record（写后重读）→ refresh_sheet → delete_records。"""
    file_id, sheet_id = sheet_ref
    cache = SheetCache(storage, sync_interval_minutes=99999)

    initial_count = await cache.add_sheet(file_id, sheet_id)
    assert initial_count >= 0  # 不假设具体条数；只验证调用通

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    test_row = {
        "类型": "翻译",
        "进度": "未分配",
        "组员": "999999999",
        "备注": f"[cache-integ {stamp}]",
    }
    inserted = await cache.add_records(file_id, sheet_id, [test_row])
    assert len(inserted) == 1
    rid = inserted[0].record_id
    try:
        # 查询走缓存
        cached = cache.get_record(file_id, sheet_id, rid)
        assert cached is not None
        assert cached.values["进度"] == "未分配"
        assert cached.values["备注"] == test_row["备注"]

        # 更新 + 写后重读 应反映最新值
        async with cache.lock_for(file_id, sheet_id, rid):
            updated = await cache.update_record(
                file_id, sheet_id, rid, {"进度": "已完成"}
            )
        assert updated.values["进度"] == "已完成"
        # 缓存里也应该是已完成（单选回填正确）
        assert cache.get_record(file_id, sheet_id, rid).values["进度"] == "已完成"  # type: ignore[union-attr]

        # refresh_sheet 应保留这条记录
        count = await cache.refresh_sheet(file_id, sheet_id)
        assert count >= 1
        assert cache.get_record(file_id, sheet_id, rid) is not None
    finally:
        await cache.delete_records(file_id, sheet_id, [rid])

    # delete 后缓存清掉
    assert cache.get_record(file_id, sheet_id, rid) is None


async def test_refresh_record_picks_up_external_change(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    """模拟"30 分钟内别人在腾讯文档手改了"的场景：
    A. cache 装上一条 record；
    B. 通过 storage 直接改它（绕过 cache，模拟外部编辑）；
    C. cache.refresh_record 应拉到新值。
    """
    file_id, sheet_id = sheet_ref
    cache = SheetCache(storage, sync_interval_minutes=99999)
    await cache.add_sheet(file_id, sheet_id)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    [rec] = await cache.add_records(
        file_id,
        sheet_id,
        [{"类型": "时轴", "进度": "未分配", "组员": "0", "备注": f"[refresh-test {stamp}]"}],
    )
    rid = rec.record_id
    try:
        # 绕过 cache 改远端
        await storage.update_record(
            file_id, sheet_id, rid, {"备注": f"[refresh-test {stamp}] external-edit"}
        )
        # cache 还没刷过 → 仍是旧值
        cached = cache.get_record(file_id, sheet_id, rid)
        assert cached is not None
        assert "external-edit" not in cached.values["备注"]

        # refresh 单条
        refreshed = await cache.refresh_record(file_id, sheet_id, rid)
        assert refreshed is not None
        assert "external-edit" in refreshed.values["备注"]
        assert "external-edit" in cache.get_record(file_id, sheet_id, rid).values["备注"]  # type: ignore[union-attr]
    finally:
        await cache.delete_records(file_id, sheet_id, [rid])
