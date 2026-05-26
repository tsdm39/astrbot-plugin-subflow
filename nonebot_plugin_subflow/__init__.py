"""字幕组任务管理 Bot — NoneBot2 插件入口。

启动顺序：
1. NoneBot 加载本插件 → 读取 Config（pydantic 校验 .env 字段）
2. on_startup → deps.init(config)：
   - 检查 access_token 过期状态（D2）；EXPIRED 时记录错误但仍启动（降级模式）
   - 构造 storage/cache/bindings/pipelines/task_manager 单例
   - 对所有已绑定子表全量拉取一次
   - 启动 30 分钟定时同步任务
3. on_shutdown → deps.teardown()：停同步任务 + 关闭 http client

命令的注册通过 import .commands 触发（M4 Part B 实现）。
"""

from __future__ import annotations

from nonebot import get_driver, get_plugin_config
from nonebot.plugin import PluginMetadata

from . import deps
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="subflow",
    description="字幕组任务管理 Bot — 对接腾讯文档智能表，QQ 群管理接活/完成/依赖",
    usage="参见 README.md 命令清单",
    type="application",
    homepage="https://github.com/wenhe753/nonebot-plugin-subflow",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

__version__ = "0.0.1"


# 在 NoneBot 环境内才注册生命周期钩子；其他场景（单测、文档构建等）容忍 import。
try:
    _driver = get_driver()
except ValueError:
    _driver = None

if _driver is not None:

    @_driver.on_startup
    async def _startup() -> None:
        # 延迟到启动时再读 Config，避免 import 时段缺字段就报错（影响测试和文档构建）
        config = get_plugin_config(Config)
        await deps.init(config)

    @_driver.on_shutdown
    async def _shutdown() -> None:
        await deps.teardown()


# 命令注册 —— import 触发 Matcher 注册到 NoneBot
from . import commands  # noqa: E402, F401
