"""
字幕组任务管理 Bot — AstrBot 插件入口。

架构说明：
- 继承 Star 基类，所有命令用 @filter.command 注册
- 业务层代码（task_manager/cache/bindings/pipeline/storage）完全复用，无需改动
- 渲染层 render.py 仅需将 MessageSegment.at 改为 [CQ:at,qq=xxx] 文本格式
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger as astr_logger

# 复用原有业务模块（完全不需要改）
from . import deps, render
from .config import Config, load_env_file, load_tencent_creds
from .exceptions import (
    BindingError,
    PipelineError,
    StorageError,
    TokenExpiredError,
)
from .task_manager import TaskError

log = logging.getLogger(__name__)


class SubflowPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._initialized = False

    async def initialize(self) -> None:
        """插件初始化：读取配置 → 构造所有单例 → 加载缓存"""
        if self._initialized:
            return

        # 1. 读取配置（从环境变量，兼容原有 .env 格式）
        cfg_dict = {}
        plugin_dir = Path(__file__).parent
        env_path = plugin_dir / ".key"
        if env_path.exists():
            cfg_dict = load_env_file(env_path)
        # 环境变量覆盖
        import os
        for key in [
            "TENCENT_DOC_CLIENT_ID", "TENCENT_DOC_OPEN_ID", "TENCENT_DOC_ACCESS_TOKEN",
            "SUBFLOW_TENCENT_DOC_KEYS", "SUBFLOW_MAIN_GROUP_ID",
            "SUBFLOW_ADMIN_QQ_LIST", "SUBFLOW_MAX_TASKS_PER_USER",
            "SUBFLOW_SYNC_INTERVAL", "SUBFLOW_CONFIRM_TIMEOUT",
            "SUBFLOW_TOKEN_WARN_DAYS", "SUBFLOW_DATA_DIR",
            "SUBFLOW_DEFAULT_PIPELINE", "SUBFLOW_NOTIFY_EXTERNAL_CHANGES",
            "SUBFLOW_EXTERNAL_CHANGE_DIGEST_THRESHOLD",
        ]:
            env_val = os.environ.get(key)
            if env_val is not None:
                cfg_dict[key.lower()] = env_val

        config = Config(**cfg_dict)
        log.info("subflow 配置加载完成")

        # 2. 初始化依赖（复用原有 deps.init）
        await deps.init(config)
        self._initialized = True
        log.info("subflow 插件初始化完成")

    # ================================================================
    # 辅助方法
    # ================================================================

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取发送者 QQ 号"""
        return event.get_sender_id()

    def _get_group_id(self, event: AstrMessageEvent) -> int | None:
        """获取群号（AstrBot 中通过 message_obj 获取）"""
        # AstrBot 的 message_obj 可能包含群信息
        # 这里根据 AstrBot 的实际 API 调整
        msg_obj = event.message_obj
        if hasattr(msg_obj, 'group_id'):
            return int(msg_obj.group_id)
        if hasattr(msg_obj, 'group_name'):
            # 有些实现可能没有直接 group_id
            return None
        return None

    def _is_group_msg(self, event: AstrMessageEvent) -> bool:
        """判断是否为群消息"""
        return self._get_group_id(event) is not None

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断用户是否为管理员（群主/群管/超管）"""
        user_id = self._get_user_id(event)
        group_id = self._get_group_id(event)

        # 超管检查
        cfg = deps.config
        if cfg and user_id:
            try:
                if int(user_id) in cfg.subflow_admin_qq_list:
                    return True
            except ValueError:
                pass

        # 群主/群管检查（需要 AstrBot 提供相应 API）
        # 在 AstrBot 中，可能需要通过 context 获取群成员信息
        # 这里作为简化，先只检查超管
        # TODO: 根据 AstrBot 的实际 API 补充群管检查
        return False

    async def _reject_if_main_group(self, event: AstrMessageEvent) -> str | None:
        """D9：总群拒绝写操作。返回 True 表示已被拒绝（应终止处理）"""
        group_id = self._get_group_id(event)
        if group_id and deps.require_bindings().is_main_group(group_id):
            return "⚠️ 写操作请到对应工作群执行，总群仅支持查询"
        return None

    def _split_args(self, event: AstrMessageEvent) -> list[str]:
        """分割消息文本为参数列表"""
        return event.message_str.strip().split()

    def _parse_segment_count(self, token: str | None) -> int:
        """D12：/新建集 X 7 3 里的 "3" → 3；省略则默认 1"""
        if token is None or token == "":
            return 1
        try:
            n = int(token)
        except ValueError:
            raise ValueError(f"分段数必须是整数：「{token}」")
        if n < 1:
            raise ValueError(f"分段数必须 ≥ 1（当前 {n}）")
        return n

    def _parse_kv(self, token: str) -> tuple[str, str]:
        """备注=加急 → (备注, 加急)"""
        if "=" not in token:
            raise ValueError(f"字段参数格式错误：{token}（应为 字段=值）")
        k, v = token.split("=", 1)
        return k.strip(), v.strip()

    def _handle_error(self, exc: Exception):
        """已知异常 → 友好中文；未知 → 日志 + 通用提示"""
        if isinstance(exc, TaskError):
            return f"⚠️ {exc}"
        if isinstance(exc, BindingError):
            return f"⚠️ 绑定错误：{exc}"
        if isinstance(exc, PipelineError):
            return f"⚠️ 流水线错误：{exc}"
        if isinstance(exc, StorageError):
            return f"⚠️ 腾讯文档错误：{exc}"
        if isinstance(exc, TokenExpiredError):
            return "⚠️ 腾讯文档 token 已过期，请联系管理员更新"
        if isinstance(exc, ValueError):
            return f"⚠️ 参数错误：{exc}"
        # 未知异常
        log.exception("未预期的错误")
        return "⚠️ 内部错误，请联系管理员"

    # ================================================================
    # 绑定相关命令
    # ================================================================

    @filter.command("绑定id")
    async def bind_id(self, event: AstrMessageEvent):
        """管理员，绑定子表"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/绑定id <fileID或URL编码ID> <sheetID> <别名>")
            return
        file_id_or_encoded = args[0]
        sheet_id = args[1]
        alias = " ".join(args[2:])

        group_id = self._get_group_id(event)
        if group_id is None:
            yield event.plain_result("⚠️ 请在群聊中使用此命令")
            return

        try:
            outcome = deps.require_bindings().bind(
                group_id=group_id,
                alias=alias,
                file_id=file_id_or_encoded,
                sheet_id=sheet_id,
            )
            # 如果是 encoded ID，需转成真实 file_id
            file_id = outcome.file_id
            # 加载到缓存
            if deps.storage:
                n = await deps.require_cache().add_sheet(file_id, sheet_id)
                log.info("loaded %d records for %s", n, alias)
            yield event.plain_result(f"✅ 绑定成功：{alias}")
        except BindingError as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("解绑")
    async def unbind(self, event: AstrMessageEvent):
        """管理员，解绑子表"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 1:
            yield event.plain_result("⚠️ 格式：/解绑 <别名>")
            return
        alias = " ".join(args)

        try:
            deps.require_bindings().unbind(alias)
            yield event.plain_result(f"✅ 已解绑：{alias}")
        except BindingError as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("绑定列表")
    async def bind_list(self, event: AstrMessageEvent):
        """查看绑定列表。全部 需超管"""
        args = self._split_args(event)
        show_all = "全部" in args

        if show_all:
            user_id = self._get_user_id(event)
            cfg = deps.config
            if cfg and user_id:
                try:
                    if int(user_id) not in cfg.subflow_admin_qq_list:
                        yield event.plain_result("⚠️ 仅超管可查看全部绑定")
                        return
                except ValueError:
                    yield event.plain_result("⚠️ 仅超管可查看全部绑定")
                    return

        group_id = self._get_group_id(event)
        entries = deps.require_bindings().list_for_group(group_id, show_all=show_all)
        if not entries:
            yield event.plain_result("当前无绑定记录" if not show_all else "全部绑定列表为空")
            return

        lines = ["📋 绑定列表："]
        for e in entries:
            lines.append(f"  {e.alias} → 群 {e.group_id}")
        yield event.plain_result("\n".join(lines))

    # ================================================================
    # 流水线相关命令
    # ================================================================

    @filter.command("设置流水线")
    async def set_pipeline(self, event: AstrMessageEvent):
        """管理员，设置流水线 DSL"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        text = event.message_str.strip()
        # 去掉命令前缀
        prefix = "/设置流水线"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
        if not text:
            yield event.plain_result("⚠️ 格式：/设置流水线 <番剧名> <DSL>")
            return

        # 第一个词是番剧名，后面是 DSL
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("⚠️ 格式：/设置流水线 <番剧名> <DSL>")
            return
        show_name = parts[0]
        dsl = parts[1]

        try:
            deps.require_pipelines().set_pipeline(show_name, dsl)
            yield event.plain_result(f"✅ 已为「{show_name}」设置流水线")
        except PipelineError as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("查看流水线")
    async def view_pipeline(self, event: AstrMessageEvent):
        """查看流水线"""
        args = self._split_args(event)
        if len(args) < 1:
            yield event.plain_result("⚠️ 格式：/查看流水线 <番剧名>")
            return
        show_name = " ".join(args)

        try:
            pipeline = deps.require_pipelines().get_pipeline(show_name)
            from .pipeline import to_dsl
            dsl = to_dsl(pipeline)
            yield event.plain_result(f"📋 「{show_name}」流水线：\n{dsl}")
        except PipelineError as e:
            yield event.plain_result(f"⚠️ {e}")

    # ================================================================
    # 集级操作命令
    # ================================================================

    @filter.command("新建集")
    async def new_episode(self, event: AstrMessageEvent):
        """管理员，新建集"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 2:
            yield event.plain_result("⚠️ 格式：/新建集 <番剧名> <集数> [分段数=1]")
            return

        show_name = args[0]
        episode = args[1]
        segment_count = self._parse_segment_count(args[2] if len(args) > 2 else None)

        try:
            outcome = deps.require_task_manager().create_episode(
                show=show_name, episode=episode, segment_count=segment_count
            )
            summary = render.render_create_episode(outcome)
            yield event.plain_result(summary)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("新建特殊")
    async def new_special(self, event: AstrMessageEvent):
        """管理员，新建特殊集（OP/ED）"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/新建特殊 <番剧名> <集数标识> <类型1> [类型2] ...")
            return

        show_name = args[0]
        episode_label = args[1]
        stages = args[2:]

        try:
            outcome = deps.require_task_manager().create_special_episode(
                show=show_name, episode=episode_label, stages=stages
            )
            summary = render.render_create_episode(outcome)
            yield event.plain_result(summary)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("删除任务")
    async def delete_task(self, event: AstrMessageEvent):
        """管理员，删除任务（需二次确认）"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 2:
            yield event.plain_result("⚠️ 格式：/删除任务 <番剧名> <集数> [类型] [分段]")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2] if len(args) > 2 else None
        segment = args[3] if len(args) > 3 else None

        try:
            summary = deps.require_task_manager().delete_task(show_name, episode, stage, segment)
            msg = render.render_delete_summary(summary)
            # 保存待确认状态到 context 中
            key = f"confirm_delete_{self._get_user_id(event)}"
            await self.context.record_handler(key, {
                "show": show_name,
                "episode": episode,
                "stage": stage,
                "segment": segment,
            })
            yield event.plain_result(msg + "\n\n发送「确认删除」以确认此操作")
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("确认删除")
    async def confirm_delete(self, event: AstrMessageEvent):
        """确认删除（二次确认）"""
        if await self._reject_if_main_group(event):
            return

        user_id = self._get_user_id(event)
        key = f"confirm_delete_{user_id}"
        
        # 获取之前保存的状态
        state = self.context.get_handler_record(key)
        if state is None:
            yield event.plain_result("⚠️ 没有待确认的删除操作，请先使用 /删除任务")
            return

        try:
            outcome = deps.require_task_manager().confirm_pending(user_id)
            msg = render.render_delete_outcome(outcome)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")
        finally:
            self.context.clear_handler_record(key)

    @filter.command("修改任务")
    async def update_task(self, event: AstrMessageEvent):
        """管理员，修改任务字段"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 4:
            yield event.plain_result("⚠️ 格式：/修改任务 <番剧名> <集数> <类型> [分段] <字段>=<值>")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2]
        # 检查最后一个参数是否是 kv
        if "=" in args[-1]:
            segment = args[3] if len(args) > 4 and "=" not in args[3] else None
            kv_token = args[-1]
        else:
            segment = args[3] if len(args) > 3 else None
            kv_token = args[4] if len(args) > 4 else ""

        try:
            field, value = self._parse_kv(kv_token)
            outcome = deps.require_task_manager().update_task(
                show=show_name, episode=episode, stage=stage,
                segment=segment, field=field, value=value,
            )
            msg = render.render_update_outcome(outcome)
            yield event.plain_result(msg)
        except (TaskError, PipelineError, ValueError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("归档")
    async def archive(self, event: AstrMessageEvent):
        """管理员，归档集"""
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可执行此操作")
            return
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 2:
            yield event.plain_result("⚠️ 格式：/归档 <番剧名> <集数>")
            return

        show_name = args[0]
        episode = args[1]

        try:
            outcomes = deps.require_task_manager().archive_episode(
                show=show_name, episode=episode
            )
            for o in outcomes:
                yield event.plain_result(render.render_archive_outcome(o))
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    # ================================================================
    # 任务操作命令
    # ================================================================

    @filter.command("接活")
    async def claim(self, event: AstrMessageEvent):
        """接活"""
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/接活 <番剧名> <集数> <类型> [分段]")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2]
        segment = args[3] if len(args) > 3 else None

        user_id = self._get_user_id(event)

        try:
            outcome = deps.require_task_manager().claim_task(
                show=show_name, episode=episode, stage=stage,
                segment=segment, user_id=user_id,
            )
            msg = render.render_claim_outcome(outcome)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("完成")
    async def complete_task(self, event: AstrMessageEvent):
        """完成任务"""
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/完成 <番剧名> <集数> <类型> [分段]")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2]
        segment = args[3] if len(args) > 3 else None

        user_id = self._get_user_id(event)

        try:
            outcome = deps.require_task_manager().complete_task(
                show=show_name, episode=episode, stage=stage,
                segment=segment, user_id=user_id,
            )
            msgs = render.render_complete_outcome(outcome)
            for msg in msgs:
                yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("放弃")
    async def abandon_task(self, event: AstrMessageEvent):
        """放弃任务"""
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/放弃 <番剧名> <集数> <类型> [分段]")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2]
        segment = args[3] if len(args) > 3 else None

        user_id = self._get_user_id(event)

        try:
            outcome = deps.require_task_manager().abandon_task(
                show=show_name, episode=episode, stage=stage,
                segment=segment, user_id=user_id,
            )
            msg = render.render_abandon_outcome(outcome)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("进行中")
    async def in_progress(self, event: AstrMessageEvent):
        """设置任务为进行中"""
        if await self._reject_if_main_group(event):
            return

        args = self._split_args(event)
        if len(args) < 3:
            yield event.plain_result("⚠️ 格式：/进行中 <番剧名> <集数> <类型> [分段]")
            return

        show_name = args[0]
        episode = args[1]
        stage = args[2]
        segment = args[3] if len(args) > 3 else None

        user_id = self._get_user_id(event)

        try:
            outcome = deps.require_task_manager().set_in_progress(
                show=show_name, episode=episode, stage=stage,
                segment=segment, user_id=user_id,
            )
            msg = render.render_in_progress_outcome(outcome)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    # ================================================================
    # 查询命令
    # ================================================================

    @filter.command("进度")
    async def progress(self, event: AstrMessageEvent):
        """查看进度看板"""
        args = self._split_args(event)
        if len(args) < 1:
            yield event.plain_result("⚠️ 格式：/进度 <番剧名> [集数]")
            return

        show_name = args[0]
        episode = args[1] if len(args) > 1 else None

        try:
            board = deps.require_task_manager().list_episode(show=show_name, episode=episode)
            msg = render.render_episode_board(records)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("我的任务")
    async def my_tasks(self, event: AstrMessageEvent):
        """查看我的任务"""
        user_id = self._get_user_id(event)

        try:
            tasks = deps.require_task_manager().list_my_tasks(user_id)
            if not tasks:
                yield event.plain_result("你目前没有未完成任务")
                return
            msg = render.render_user_tasks(tasks, user_id)
            yield event.plain_result(msg)
        except TaskError as e:
            yield event.plain_result(f"⚠️ {e}")

    @filter.command("待接")
    async def pending(self, event: AstrMessageEvent):
        """查看待接任务列表"""
        args = self._split_args(event)
        show_name = args[0] if args else None

        try:
            tasks = deps.require_task_manager().list_available(show=show_name)
            if not tasks:
                yield event.plain_result("当前没有待接任务" if not show_name else f"「{show_name}」没有待接任务")
                return
            msg = render.render_pending_tasks(tasks)
            yield event.plain_result(msg)
        except (TaskError, PipelineError) as e:
            yield event.plain_result(f"⚠️ {e}")

    # ================================================================
    # 生命周期
    # ================================================================

    async def terminate(self):
        """插件被卸载/停用时调用"""
        log.info("subflow 插件正在关闭...")
        await deps.teardown()
        log.info("subflow 插件已关闭")
