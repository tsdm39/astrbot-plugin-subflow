"""权限模型（D4）。

- SUBFLOW_SUPER_ADMIN : 仅 SUBFLOW_ADMIN_QQ_LIST 内的 QQ；用于跨群命令（如 /绑定列表 全部）
- SUBFLOW_ADMIN      : 群主 / 群管 / SUBFLOW_ADMIN_QQ_LIST 三者并集；
                       用于群内管理命令（绑定、新建集、删除任务、归档...）

NoneBot 的 Permission 是检查回调的组合，可以用 | 拼接。
"""

from __future__ import annotations

from typing import Awaitable, Callable, Iterable

from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import GROUP_ADMIN, GROUP_OWNER
from nonebot.permission import Permission


CheckFn = Callable[[Bot, Event], Awaitable[bool]]


def make_super_admin_check(super_admin_qqs: Iterable[int]) -> CheckFn:
    """返回独立的 check 函数（暴露出来便于单元测试，不经 NoneBot DI）。"""
    allowed = {int(q) for q in super_admin_qqs}

    async def _check(bot: Bot, event: Event) -> bool:
        try:
            return int(event.get_user_id()) in allowed
        except (ValueError, NotImplementedError):
            return False

    return _check


def make_super_admin_permission(super_admin_qqs: Iterable[int]) -> Permission:
    """只允许 super_admin_qqs 内 QQ 通过的 Permission。"""
    return Permission(make_super_admin_check(super_admin_qqs))


def make_admin_permission(super_admin_qqs: Iterable[int]) -> Permission:
    """群主 ∪ 群管 ∪ super_admins。"""
    return GROUP_OWNER | GROUP_ADMIN | make_super_admin_permission(super_admin_qqs)
