"""commands.py 单元测试 — 限于纯解析 helpers + 异常映射 + matcher 注册 smoke。

完整的命令端到端流程（带 NoneBot Bot/Event 模拟）应当在真实 QQ 群里手测，
不在单元测试覆盖范围。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.exception import FinishedException, MatcherException

from nonebot_plugin_subflow import commands
from nonebot_plugin_subflow.commands import (
    _parse_kv,
    _parse_segments,
    _send_user_error,
    _split_args,
)
from nonebot_plugin_subflow.exceptions import (
    AliasConflictError,
    PipelineError,
    StorageError,
    TokenExpiredError,
)
from nonebot_plugin_subflow.task_manager import TaskNotFoundError


# ============================================================ helper: _split_args


def test_split_args_extracts_plain_text() -> None:
    from nonebot.adapters.onebot.v11 import Message

    msg = Message("  淡岛百景 07 翻译 P1  ")
    assert _split_args(msg) == ["淡岛百景", "07", "翻译", "P1"]


def test_split_args_empty() -> None:
    from nonebot.adapters.onebot.v11 import Message

    assert _split_args(Message("")) == []


# ============================================================ _parse_segments


def test_parse_segments_basic() -> None:
    assert _parse_segments(["P1=0-8", "P2=8-16", "P3=16-END"]) == {
        "P1": "0-8",
        "P2": "8-16",
        "P3": "16-END",
    }


def test_parse_segments_strips_whitespace() -> None:
    assert _parse_segments(["P1 = 0-8 "]) == {"P1": "0-8"}


def test_parse_segments_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="分段参数"):
        _parse_segments(["P1"])


def test_parse_segments_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="分段参数为空"):
        _parse_segments(["P1="])
    with pytest.raises(ValueError, match="分段参数为空"):
        _parse_segments(["=0-8"])


# ============================================================ _parse_kv


def test_parse_kv_basic() -> None:
    assert _parse_kv("备注=加急") == ("备注", "加急")


def test_parse_kv_keeps_equals_in_value() -> None:
    assert _parse_kv("note=a=b") == ("note", "a=b")


def test_parse_kv_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match="字段参数"):
        _parse_kv("备注")


# ============================================================ _send_user_error


async def test_send_user_error_propagates_matcher_exception() -> None:
    """MatcherException（finish/reject）必须 re-raise，否则 finish 不生效。"""
    matcher = MagicMock()
    matcher.send = AsyncMock()
    with pytest.raises(FinishedException):
        await _send_user_error(matcher, FinishedException())
    matcher.send.assert_not_awaited()


async def test_send_user_error_token_expired_message() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, TokenExpiredError("expired"))
    sent_text = matcher.send.call_args[0][0]
    assert "access_token 已失效" in sent_text


async def test_send_user_error_task_error_passes_through_message() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, TaskNotFoundError("找不到任务：xxx"))
    sent_text = matcher.send.call_args[0][0]
    assert "❌" in sent_text
    assert "找不到任务" in sent_text


async def test_send_user_error_binding_error_passes_through() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, AliasConflictError("别名冲突"))
    assert "别名冲突" in matcher.send.call_args[0][0]


async def test_send_user_error_pipeline_error_message() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, PipelineError("流水线 DSL 错"))
    assert "流水线 DSL 错" in matcher.send.call_args[0][0]


async def test_send_user_error_storage_error_marks_remote() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, StorageError("ret 500 internal"))
    assert "远端错误" in matcher.send.call_args[0][0]


async def test_send_user_error_unknown_exception_internal() -> None:
    matcher = MagicMock()
    matcher.send = AsyncMock()
    await _send_user_error(matcher, RuntimeError("oops"))
    assert "内部错误" in matcher.send.call_args[0][0]


# ============================================================ matcher registration smoke


def test_all_expected_matchers_registered() -> None:
    """import commands.py 后，关键 matcher 全部应存在为模块属性。"""
    expected = [
        "bind_matcher",
        "bind_id_matcher",
        "unbind_matcher",
        "bindings_list_matcher",
        "set_pipeline_matcher",
        "view_pipeline_matcher",
        "create_episode_matcher",
        "create_special_matcher",
        "delete_task_matcher",
        "confirm_delete_matcher",
        "update_task_matcher",
        "archive_matcher",
        "claim_matcher",
        "complete_matcher",
        "abandon_matcher",
        "in_progress_matcher",
        "progress_matcher",
        "my_tasks_matcher",
        "available_matcher",
    ]
    for name in expected:
        assert hasattr(commands, name), f"missing matcher: {name}"


def test_super_admin_permission_objects_exist() -> None:
    from nonebot.permission import Permission

    assert isinstance(commands.SUBFLOW_SUPER_ADMIN, Permission)
    assert isinstance(commands.SUBFLOW_ADMIN, Permission)
