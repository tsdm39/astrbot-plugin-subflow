"""M1 阶段的最小配置读取。

故意不引入 pydantic-settings：M4 接入 NoneBot2 后，会换成 NoneBot 标准的 Config 模型，
本模块只在脱离 NoneBot 跑测试时使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TencentCreds:
    client_id: str
    open_id: str
    access_token: str


def load_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 格式的 .env，忽略空行/注释。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def load_tencent_creds(env_file: Path | None = None) -> TencentCreds:
    """优先 process env，再读 env_file。"""
    file_env = load_env_file(env_file) if env_file else {}

    def _get(key: str) -> str:
        v = os.environ.get(key) or file_env.get(key)
        if not v:
            raise RuntimeError(f"missing required config: {key}")
        return v

    return TencentCreds(
        client_id=_get("TENCENT_DOC_CLIENT_ID"),
        open_id=_get("TENCENT_DOC_OPEN_ID"),
        access_token=_get("TENCENT_DOC_ACCESS_TOKEN"),
    )
