"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import nonebot
import pytest
import pytest_asyncio

# 初始化 NoneBot driver，让插件可以被 import；on_startup/on_shutdown 不会自动触发，
# 所以 deps 不会真的初始化，单元测试该有的 monkeypatch / 显式构造仍走自己的路径。
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from nonebot_plugin_subflow.config import load_env_file  # noqa: E402
from nonebot_plugin_subflow.storage import TencentDocStorage  # noqa: E402


SPIKE_ENV_PATH = Path(__file__).resolve().parents[1] / "spike" / ".env.spike"


@pytest.fixture(scope="session")
def spike_env() -> dict[str, str]:
    """读取 spike/.env.spike 给集成测试用。文件不存在时跳过 — pytest 命令行可用 -m 'not integration' 离线跑。"""
    if not SPIKE_ENV_PATH.exists():
        pytest.skip(f"spike credentials not found at {SPIKE_ENV_PATH}")
    env = load_env_file(SPIKE_ENV_PATH)
    required = [
        "TENCENT_DOC_CLIENT_ID",
        "TENCENT_DOC_OPEN_ID",
        "TENCENT_DOC_ACCESS_TOKEN",
        "TENCENT_DOC_FILE_ID",
        "TENCENT_DOC_SHEET_ID",
    ]
    missing = [k for k in required if not env.get(k)]
    if missing:
        pytest.skip(f"spike/.env.spike missing keys: {missing}")
    return env


@pytest_asyncio.fixture
async def storage(spike_env: dict[str, str]) -> AsyncIterator[TencentDocStorage]:
    backend = TencentDocStorage(
        client_id=spike_env["TENCENT_DOC_CLIENT_ID"],
        open_id=spike_env["TENCENT_DOC_OPEN_ID"],
        access_token=spike_env["TENCENT_DOC_ACCESS_TOKEN"],
    )
    try:
        yield backend
    finally:
        await backend.aclose()


@pytest.fixture
def sheet_ref(spike_env: dict[str, str]) -> tuple[str, str]:
    return spike_env["TENCENT_DOC_FILE_ID"], spike_env["TENCENT_DOC_SHEET_ID"]
