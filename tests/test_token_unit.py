"""token.py 单元测试。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nonebot_plugin_subflow.config import load_env_file
from nonebot_plugin_subflow.token import (
    InvalidTokenError,
    TokenStatus,
    check_token_status,
    decode_jwt_payload,
    format_status,
    get_expiry,
)


def _make_jwt(payload: dict) -> str:
    """造一个 payload 已知的 JWT（header/signature 占位即可，我们不验签）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps(payload).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.signature"


# ------------------------------------------------------- decode


def test_decode_jwt_payload_extracts_exp() -> None:
    token = _make_jwt({"exp": 1782287866, "sub": "x"})
    payload = decode_jwt_payload(token)
    assert payload["exp"] == 1782287866
    assert payload["sub"] == "x"


def test_decode_jwt_payload_rejects_non_jwt() -> None:
    with pytest.raises(InvalidTokenError):
        decode_jwt_payload("not.a.token.too.many.parts")
    with pytest.raises(InvalidTokenError):
        decode_jwt_payload("only_one_segment")


def test_decode_jwt_payload_rejects_garbage_payload() -> None:
    with pytest.raises(InvalidTokenError):
        decode_jwt_payload("aaa.!!!notb64!!!.ccc")


def test_get_expiry_raises_without_exp_claim() -> None:
    token = _make_jwt({"sub": "x"})  # no exp
    with pytest.raises(InvalidTokenError):
        get_expiry(token)


def test_get_expiry_returns_datetime() -> None:
    ts = 1782287866
    token = _make_jwt({"exp": ts})
    assert get_expiry(token) == datetime.fromtimestamp(ts)


# ------------------------------------------------------- status


def test_check_status_valid_when_far_from_expiry() -> None:
    future = datetime.now() + timedelta(days=30)
    token = _make_jwt({"exp": future.timestamp()})
    check = check_token_status(token, warn_days=7)
    assert check.status is TokenStatus.VALID


def test_check_status_expiring_within_warn_window() -> None:
    future = datetime.now() + timedelta(days=3)
    token = _make_jwt({"exp": future.timestamp()})
    check = check_token_status(token, warn_days=7)
    assert check.status is TokenStatus.EXPIRING_SOON


def test_check_status_expired() -> None:
    past = datetime.now() - timedelta(hours=1)
    token = _make_jwt({"exp": past.timestamp()})
    check = check_token_status(token, warn_days=7)
    assert check.status is TokenStatus.EXPIRED
    assert check.remaining.total_seconds() < 0


def test_format_status_messages() -> None:
    valid_token = _make_jwt({"exp": (datetime.now() + timedelta(days=30)).timestamp()})
    soon_token = _make_jwt({"exp": (datetime.now() + timedelta(days=3)).timestamp()})
    expired_token = _make_jwt({"exp": (datetime.now() - timedelta(days=1)).timestamp()})

    assert "有效" in format_status(check_token_status(valid_token))
    assert "天后过期" in format_status(check_token_status(soon_token))
    assert "已过期" in format_status(check_token_status(expired_token))


# ------------------------------------------------------- real token (if available)


SPIKE_ENV_PATH = Path(__file__).resolve().parents[1] / "spike" / ".env.spike"


def test_real_spike_token_decodes() -> None:
    """如果 spike/.env.spike 存在，确认真实 token 也能解析。"""
    if not SPIKE_ENV_PATH.exists():
        pytest.skip("no spike credentials available")
    env = load_env_file(SPIKE_ENV_PATH)
    token = env.get("TENCENT_DOC_ACCESS_TOKEN")
    if not token:
        pytest.skip("TENCENT_DOC_ACCESS_TOKEN not set")
    check = check_token_status(token, warn_days=7)
    # 不断言具体状态（可能 VALID 或 EXPIRING_SOON 视测试日子而定），但 expires_at 应在过去半年到未来 2 年范围内
    now = datetime.now()
    assert (now - timedelta(days=180)) < check.expires_at < (now + timedelta(days=730))
