"""permissions.py 单元测试。

NoneBot 的 Permission 是 callable check 的组合。我们只测自己写的 super_admin 检查；
GROUP_OWNER / GROUP_ADMIN 由 NoneBot 自己负责，不在这里覆盖。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nonebot_plugin_subflow.permissions import (
    make_admin_permission,
    make_super_admin_check,
    make_super_admin_permission,
)


@pytest.fixture
def event_factory():
    def _make(user_id: str):
        event = MagicMock()
        event.get_user_id.return_value = user_id
        return event

    return _make


@pytest.fixture
def bot() -> MagicMock:
    return MagicMock()


# ============================================================ super admin check (raw fn)


async def test_super_admin_check_allows_whitelisted(event_factory, bot) -> None:
    check = make_super_admin_check([100, 200])
    assert await check(bot, event_factory("100")) is True
    assert await check(bot, event_factory("200")) is True


async def test_super_admin_check_denies_non_whitelisted(event_factory, bot) -> None:
    check = make_super_admin_check([100, 200])
    assert await check(bot, event_factory("999")) is False


async def test_super_admin_check_handles_non_numeric_user_id(
    event_factory, bot
) -> None:
    check = make_super_admin_check([100])
    assert await check(bot, event_factory("not_a_number")) is False


async def test_empty_super_admin_check_denies_everyone(event_factory, bot) -> None:
    check = make_super_admin_check([])
    assert await check(bot, event_factory("100")) is False


# ============================================================ permission wrapping smoke


def test_make_super_admin_permission_returns_permission_instance() -> None:
    from nonebot.permission import Permission

    perm = make_super_admin_permission([100])
    assert isinstance(perm, Permission)


def test_make_admin_permission_returns_permission_instance() -> None:
    from nonebot.permission import Permission

    perm = make_admin_permission([100])
    assert isinstance(perm, Permission)
