"""access_token JWT 解析与过期检查（D2）。

Bot 启动时用 `check_token_status` 判断当前 token 状态：
- VALID       : 还远未过期，正常启动
- EXPIRING_SOON: 距过期 ≤ warn_days，启动后立刻提醒运营
- EXPIRED     : 已过期，拒绝启动

不做签名校验 — token 是腾讯文档签的，我们没有验签密钥；只取 payload 里的 `exp`。
"""

from __future__ import annotations

import base64
import enum
import json
from dataclasses import dataclass
from datetime import datetime, timedelta


class TokenStatus(enum.Enum):
    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"


@dataclass(frozen=True)
class TokenCheck:
    status: TokenStatus
    expires_at: datetime
    remaining: timedelta  # 距过期还有多久；过期为负


class InvalidTokenError(ValueError):
    pass


def decode_jwt_payload(token: str) -> dict:
    """只解 payload，不验签。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidTokenError(f"not a JWT (got {len(parts)} segments)")
    payload_b64 = parts[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidTokenError(f"failed to decode payload: {exc}") from exc


def get_expiry(token: str) -> datetime:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if exp is None:
        raise InvalidTokenError("no 'exp' claim in JWT payload")
    if not isinstance(exp, (int, float)):
        raise InvalidTokenError(f"'exp' claim is not numeric: {exp!r}")
    return datetime.fromtimestamp(exp)


def check_token_status(
    token: str, *, warn_days: int = 7, now: datetime | None = None
) -> TokenCheck:
    expiry = get_expiry(token)
    current = now or datetime.now()
    remaining = expiry - current
    if remaining.total_seconds() <= 0:
        return TokenCheck(TokenStatus.EXPIRED, expiry, remaining)
    if remaining <= timedelta(days=warn_days):
        return TokenCheck(TokenStatus.EXPIRING_SOON, expiry, remaining)
    return TokenCheck(TokenStatus.VALID, expiry, remaining)


def format_status(check: TokenCheck) -> str:
    """生成给运营看的提示文案（用于日志/总群通知）。"""
    expiry_str = check.expires_at.strftime("%Y-%m-%d %H:%M")
    if check.status is TokenStatus.EXPIRED:
        return f"⚠️ 腾讯文档 access_token 已过期（{expiry_str}），请到开放平台后台重新生成并更新 .env"
    if check.status is TokenStatus.EXPIRING_SOON:
        days = check.remaining.days
        return f"⚠️ 腾讯文档 access_token 将在 {days} 天后过期（{expiry_str}），请尽快续期"
    return f"腾讯文档 access_token 有效（{expiry_str} 过期）"
