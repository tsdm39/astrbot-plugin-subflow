"""NoneBot2 启动脚本 — Docker 和裸机部署的统一入口。

用法：
    python bot.py

环境变量从 .env 自动加载（NoneBot 内置 pydantic-settings 行为）。
"""

from __future__ import annotations

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def main() -> None:
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("nonebot_plugin_subflow")
    nonebot.run()


if __name__ == "__main__":
    main()
