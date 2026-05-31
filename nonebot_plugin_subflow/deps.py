"""插件级单例容器。

NoneBot 是单进程多协程的，模块级全局变量足够。
init() 在 on_startup 钩子里跑，teardown() 在 on_shutdown 钩子里跑。

要在命令处理函数里取到这些单例，用 require_storage() / require_cache() / ...
未初始化时抛 RuntimeError，便于 fail-fast。
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import render
from .bindings import BindingStore
from .cache import SheetCache, SheetDiff
from .config import Config
from .pipeline import PipelineStore
from .storage import TencentDocStorage
from .task_manager import TaskManager
from .token import TokenCheck, TokenStatus, check_token_status


log = logging.getLogger(__name__)


config: Config | None = None
storage: TencentDocStorage | None = None
cache: SheetCache | None = None
bindings: BindingStore | None = None
pipelines: PipelineStore | None = None
task_manager: TaskManager | None = None
token_check_result: TokenCheck | None = None


async def init(cfg: Config) -> None:
    """启动时调用：构造所有单例，做 token 检查，加载已绑定子表到缓存。"""
    global config, storage, cache, bindings, pipelines, task_manager, token_check_result

    config = cfg

    # D18：逐 key 校验 token，剔除过期/无效，保留有效子集
    resolved = cfg.resolved_keys
    valid_keys: list = []
    token_check_result = None
    if not resolved:
        log.error(
            "未配置任何腾讯文档凭据（SUBFLOW_TENCENT_DOC_KEYS 或 "
            "TENCENT_DOC_CLIENT_ID/OPEN_ID/ACCESS_TOKEN），storage 将不可用"
        )
    for key in resolved:
        try:
            chk = check_token_status(
                key.access_token, warn_days=cfg.subflow_token_warn_days
            )
        except Exception as exc:  # noqa: BLE001  非 JWT / 解析失败
            log.error(
                "腾讯文档 key（open_id=%s）token 无法解析，剔除：%s",
                key.open_id, exc,
            )
            continue
        if token_check_result is None:
            token_check_result = chk
        if chk.status is TokenStatus.EXPIRED:
            log.error(
                "腾讯文档 key（open_id=%s）token 已过期（%s），剔除出池",
                key.open_id, chk.expires_at,
            )
            continue
        if chk.status is TokenStatus.EXPIRING_SOON:
            log.warning(
                "腾讯文档 key（open_id=%s）token 将在 %s 后过期（%s）",
                key.open_id, chk.remaining, chk.expires_at,
            )
        valid_keys.append(key)

    if resolved and not valid_keys:
        log.error(
            "所有腾讯文档 key 的 token 均已过期/无效，将以降级模式启动 — 写操作会失败"
        )

    data_dir = Path(config.subflow_data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    storage = (
        TencentDocStorage(
            keys=(valid_keys or resolved),
            rate_limit_rets=set(cfg.subflow_tencent_doc_rate_limit_rets),
            key_cooldown_seconds=cfg.subflow_tencent_doc_key_cooldown,
        )
        if resolved
        else None
    )
    cache = SheetCache(
        storage, sync_interval_minutes=cfg.subflow_sync_interval
    )
    # D17：定时同步算出 diff 后，把外部变更播报到对应工作群
    cache.on_sync_changes = _on_sync_changes
    bindings = BindingStore.load(
        data_dir / "bindings.json",
        main_group_id=cfg.subflow_main_group_id,
    )
    pipelines = PipelineStore.load(
        config_path=data_dir / "pipelines.json",
        snapshot_path=data_dir / "episode_pipelines.json",
        default_pipeline_dsl=cfg.subflow_default_pipeline,
    )
    task_manager = TaskManager(
        cache=cache,
        bindings=bindings,
        pipelines=pipelines,
        max_tasks_per_user=cfg.subflow_max_tasks_per_user,
        confirm_timeout_seconds=cfg.subflow_confirm_timeout,
    )

    # 装填缓存：有有效 key 才拉取 + 起定时同步；否则降级（查询走空缓存、写失败）
    if valid_keys:
        for entry in bindings.list_all():
            try:
                n = await cache.add_sheet(entry.file_id, entry.sheet_id)
                log.info(
                    "loaded %d records for %s (%s/%s)",
                    n,
                    entry.alias,
                    entry.file_id,
                    entry.sheet_id,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to load sheet for binding %s", entry.alias
                )
        await cache.start()


async def teardown() -> None:
    """关停：停同步任务 + 释放 http client。"""
    global storage, cache
    if cache is not None:
        await cache.stop()
    if storage is not None:
        await storage.aclose()


async def _on_sync_changes(diffs: dict[tuple[str, str], SheetDiff]) -> None:
    """D17：定时同步检测到的外部表格变更 → 渲染并推送到对应工作群。

    尽力而为：无 bot 连接 / 单群失败都只 log，不影响同步循环（cache 已包了一层）。
    """
    if config is None or not config.subflow_notify_external_changes:
        return
    if bindings is None or task_manager is None:
        return
    try:
        from nonebot import get_bot

        bot = get_bot()
    except Exception:  # noqa: BLE001
        log.warning("外部变更提醒：当前无可用 bot 连接，跳过本轮推送")
        return

    threshold = config.subflow_external_change_digest_threshold
    for (file_id, sheet_id), diff in diffs.items():
        entry = bindings.get_by_sheet(file_id, sheet_id)
        if entry is None:
            continue  # 未绑定的子表（理论上不会进到这）
        try:
            report = task_manager.interpret_external_changes(entry.alias, diff)
            if report.is_empty():
                continue
            messages = render.render_external_changes(
                report, digest_threshold=threshold
            )
            for msg in messages:
                await bot.send_group_msg(group_id=entry.group_id, message=msg)
        except Exception:  # noqa: BLE001
            log.exception("外部变更提醒推送失败：%s", entry.alias)


def require_task_manager() -> TaskManager:
    if task_manager is None:
        raise RuntimeError("subflow 插件未完成初始化（task_manager 为空）")
    return task_manager


def require_bindings() -> BindingStore:
    if bindings is None:
        raise RuntimeError("subflow 插件未完成初始化（bindings 为空）")
    return bindings


def require_pipelines() -> PipelineStore:
    if pipelines is None:
        raise RuntimeError("subflow 插件未完成初始化（pipelines 为空）")
    return pipelines


def require_storage() -> TencentDocStorage:
    if storage is None:
        raise RuntimeError("subflow 插件未完成初始化（storage 为空）")
    return storage


def require_cache() -> SheetCache:
    if cache is None:
        raise RuntimeError("subflow 插件未完成初始化（cache 为空）")
    return cache
