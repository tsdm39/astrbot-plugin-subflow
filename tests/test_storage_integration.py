"""Integration tests: 打真实腾讯文档 API。需要 spike/.env.spike。

跑：
    pytest tests/test_storage_integration.py
跳过：
    pytest -m "not integration"
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from nonebot_plugin_subflow.exceptions import UnknownColumnError
from nonebot_plugin_subflow.storage import TencentDocStorage


pytestmark = pytest.mark.integration


# ---------------------------------------------------------- read-only smoke


async def test_get_fields_returns_known_schema(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    """子表至少应含「类型」、「进度」（单选）、「组员」、「备注」（文本）这 4 列。"""
    file_id, sheet_id = sheet_ref
    fields = await storage.get_fields(file_id, sheet_id)
    by_title = {f.title: f for f in fields}

    assert "类型" in by_title and by_title["类型"].type == "single_select"
    assert "进度" in by_title and by_title["进度"].type == "single_select"
    assert "组员" in by_title and by_title["组员"].type == "text"
    assert "备注" in by_title and by_title["备注"].type == "text"

    # 进度选项至少包含基本三态
    progress_opts = by_title["进度"].options or ()
    for required_opt in ("未分配", "已分配", "已完成"):
        assert required_opt in progress_opts, f"缺少进度选项 {required_opt}"


async def test_get_fields_uses_cache(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    file_id, sheet_id = sheet_ref
    first = await storage.get_fields(file_id, sheet_id)
    second = await storage.get_fields(file_id, sheet_id)
    assert first is second  # 同一对象，证明走了缓存


async def test_get_records_returns_records_with_typed_values(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    file_id, sheet_id = sheet_ref
    records = await storage.get_records(file_id, sheet_id)
    # 用户已有数据；如果空也 OK（不报错）
    for rec in records:
        assert rec.record_id
        # 单选列若有值应为 str，不是 dict
        for col in ("类型", "进度"):
            v = rec.values.get(col)
            assert v is None or isinstance(v, str), f"{col}={v!r}"


# ---------------------------------------------------------- converter


async def test_convert_encoded_id_returns_real_file_id(
    storage: TencentDocStorage, spike_env: dict[str, str]
) -> None:
    """spike/.env.spike 里 TENCENT_DOC_FILE_ID 已是真 file_id（300000000$xxx 形式）。
    我们把它的 encoded form (注释里) 拿出来验证。
    """
    encoded = "DUmRFZmFwcnNCcEZv"  # 来自 spike/.env.spike 注释
    expected = spike_env["TENCENT_DOC_FILE_ID"]
    if not expected.startswith("300000000$"):
        pytest.skip("env file_id 不是真 file_id 形式，跳过 converter 测试")
    result = await storage.convert_encoded_id(encoded)
    assert result == expected
    # 第二次调用走缓存
    again = await storage.convert_encoded_id(encoded)
    assert again == expected


# ---------------------------------------------------------- CRUD cycle


@pytest.fixture
def test_row() -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "类型": "翻译",
        "进度": "未分配",
        "组员": f"999999999",
        "备注": f"[integ-test {stamp}]",
    }


async def test_add_update_delete_cycle(
    storage: TencentDocStorage,
    sheet_ref: tuple[str, str],
    test_row: dict[str, Any],
) -> None:
    """完整的写-读-改-删循环。任何步骤失败都要在 finally 里清理。"""
    file_id, sheet_id = sheet_ref
    inserted = await storage.add_records(file_id, sheet_id, [test_row])
    assert len(inserted) == 1
    record_id = inserted[0].record_id
    try:
        # 单选写入用 [{"text":"翻译"}] 后，add 响应应能解析回 "翻译"
        assert inserted[0].values["类型"] == "翻译"
        assert inserted[0].values["进度"] == "未分配"
        assert inserted[0].values["组员"] == "999999999"
        assert inserted[0].values["备注"] == test_row["备注"]

        # get_record 能找到刚插的
        fetched = await storage.get_record(file_id, sheet_id, record_id)
        assert fetched is not None
        assert fetched.record_id == record_id
        assert fetched.values["进度"] == "未分配"

        # update 单选 + 文本
        updated = await storage.update_record(
            file_id,
            sheet_id,
            record_id,
            {"进度": "已完成", "备注": test_row["备注"] + " updated"},
        )
        # update 响应单选回的是 option_id；借助 option_id_to_text 应解析为「已完成」
        assert updated.values["进度"] == "已完成"
        assert updated.values["备注"] == test_row["备注"] + " updated"

        # 重读确认落库
        reread = await storage.get_record(file_id, sheet_id, record_id)
        assert reread is not None
        assert reread.values["进度"] == "已完成"
    finally:
        await storage.delete_records(file_id, sheet_id, [record_id])

    # 删除后应找不到
    gone = await storage.get_record(file_id, sheet_id, record_id)
    assert gone is None


async def test_add_with_unknown_column_raises(
    storage: TencentDocStorage, sheet_ref: tuple[str, str]
) -> None:
    file_id, sheet_id = sheet_ref
    with pytest.raises(UnknownColumnError):
        await storage.add_records(
            file_id, sheet_id, [{"绝对不存在的列": "x"}]
        )
