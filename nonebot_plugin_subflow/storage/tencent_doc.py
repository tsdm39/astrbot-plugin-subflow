"""腾讯文档智能表 storage 后端。基于 spike 验证的接口约定。

关键事实（见 md/Bot实现方案.md 一节）：
- 三头部认证：Access-Token / Client-Id / Open-Id
- 所有 record/field 操作走同一端点 POST /openapi/smartbook/v2/files/{fileID}/sheets/{sheetID}，
  body 顶层 verb 区分（addRecords / getRecords / ...）
- URL 里的 encodedID 不能直接当 fileID；先调 GET /openapi/drive/v2/util/converter 转换
- 单选写入用 [{"text": "选项名"}]；裸字符串会被静默丢弃
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from ..exceptions import (
    RecordNotFoundError,
    StorageError,
    TokenExpiredError,
    UnknownColumnError,
)
from ..models import FieldSchema, Record
from .base import StorageBackend


log = logging.getLogger(__name__)


@dataclass
class _KeyState:
    """凭据池里的一套 key + 健康态（D18）。"""

    client_id: str
    open_id: str
    access_token: str
    dead: bool = False                    # token 失效，整轮出池（直到重启/续期）
    cooldown_until: datetime | None = None  # 限流冷却到期时间
    calls: int = 0                        # 已发起调用数（仅诊断）


_FIELD_TYPE_MAP: dict[int, str] = {
    1: "text",
    2: "number",
    4: "datetime",
    17: "single_select",
}

# token-过期/无效相关 ret 码。腾讯文档没公布完整列表，凭经验列；命中即抛 TokenExpiredError。
_TOKEN_ERROR_RETS: set[int] = {
    10010,  # invalid token
    10011,  # token expired
    10012,  # token not authorized
    10013,  # token signature error
}


class TencentDocStorage(StorageBackend):
    BASE_URL = "https://docs.qq.com"
    _SHEET_ENDPOINT = "/openapi/smartbook/v2/files/{file_id}/sheets/{sheet_id}"
    _CONVERTER_ENDPOINT = "/openapi/drive/v2/util/converter"
    _DEFAULT_PAGE_SIZE = 200

    def __init__(
        self,
        *,
        keys: list[Any] | None = None,
        client_id: str | None = None,
        open_id: str | None = None,
        access_token: str | None = None,
        rate_limit_rets: set[int] | None = None,
        key_cooldown_seconds: int = 60,
        timeout: float = 15.0,
    ) -> None:
        """凭据池（D18）。

        - 多 key：`keys=[TencentDocKey | dict | (client_id, open_id, access_token), ...]`
        - 单 key 兼容：`client_id=/open_id=/access_token=`（包成 size-1 池）
        """
        if keys is None:
            if not (client_id and open_id and access_token):
                raise ValueError(
                    "TencentDocStorage 需要 keys，或 client_id/open_id/access_token 三元组"
                )
            keys = [(client_id, open_id, access_token)]
        self._keys: list[_KeyState] = [self._to_state(k) for k in keys]
        if not self._keys:
            raise ValueError("TencentDocStorage 至少需要一套凭据")
        self._rate_limit_rets: set[int] = set(rate_limit_rets or ())
        self._key_cooldown = timedelta(seconds=key_cooldown_seconds)
        self._cursor = 0
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._fields_cache: dict[tuple[str, str], list[FieldSchema]] = {}
        self._fileid_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ infra

    @staticmethod
    def _to_state(k: Any) -> _KeyState:
        if isinstance(k, _KeyState):
            return k
        if isinstance(k, dict):
            return _KeyState(k["client_id"], k["open_id"], k["access_token"])
        if isinstance(k, (tuple, list)):
            return _KeyState(k[0], k[1], k[2])
        # duck-typed（如 config.TencentDocKey）
        return _KeyState(k.client_id, k.open_id, k.access_token)

    @staticmethod
    def _headers_for(state: _KeyState) -> dict[str, str]:
        return {
            "Access-Token": state.access_token,
            "Client-Id": state.client_id,
            "Open-Id": state.open_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _pick_key(self) -> _KeyState | None:
        """从游标起 round-robin 找第一个可用（非 dead、未冷却）key 并进位；全不可用返回 None。"""
        now = datetime.now()
        n = len(self._keys)
        for i in range(n):
            idx = (self._cursor + i) % n
            state = self._keys[idx]
            if state.dead:
                continue
            if state.cooldown_until is not None:
                if state.cooldown_until > now:
                    continue
                state.cooldown_until = None  # 冷却结束，恢复
            self._cursor = (idx + 1) % n
            return state
        return None

    async def _request_with_failover(
        self, send: Callable[[dict[str, str]], Awaitable[httpx.Response]]
    ) -> dict[str, Any]:
        """选 key 发请求并解包；token/限流类拒绝换把重试（最多扫一遍池），网络异常直接抛。"""
        last_exc: StorageError | None = None
        for _ in range(len(self._keys)):
            state = self._pick_key()
            if state is None:
                break
            state.calls += 1
            # 网络层异常（超时/连接错）在此抛出，不被捕获 → 直接冒泡，不重试（防重复落库）
            resp = await send(self._headers_for(state))
            try:
                return self._handle(resp)
            except TokenExpiredError as exc:
                state.dead = True
                last_exc = exc
                log.warning(
                    "腾讯文档 key（open_id=%s）token 失效，出池：%s", state.open_id, exc
                )
                continue
            except StorageError as exc:
                if exc.ret is not None and exc.ret in self._rate_limit_rets:
                    state.cooldown_until = datetime.now() + self._key_cooldown
                    last_exc = exc
                    log.warning(
                        "腾讯文档 key（open_id=%s）触发限流 ret=%s，冷却 %.0fs",
                        state.open_id, exc.ret, self._key_cooldown.total_seconds(),
                    )
                    continue
                raise  # 其它 API 错误（参数错/找不到/未知 ret）直接抛
        if last_exc is not None:
            raise last_exc
        raise StorageError("无可用的腾讯文档凭据（全部 token 失效或限流冷却中）")

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "TencentDocStorage":
        await self._client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _handle(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except Exception as exc:
            raise StorageError(
                f"non-JSON response HTTP {resp.status_code}: {resp.text[:200]}"
            ) from exc
        ret = payload.get("ret")
        if ret == 0:
            return payload.get("data") or {}
        msg = payload.get("msg", "")
        if ret in _TOKEN_ERROR_RETS:
            raise TokenExpiredError(f"token error ret={ret}: {msg}", ret=ret)
        raise StorageError(f"API error ret={ret}: {msg}", ret=ret)

    async def _call_sheet(
        self, file_id: str, sheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        url = self.BASE_URL + self._SHEET_ENDPOINT.format(
            file_id=file_id, sheet_id=sheet_id
        )
        client = await self._client()

        async def send(headers: dict[str, str]) -> httpx.Response:
            return await client.post(url, headers=headers, json=body)

        return await self._request_with_failover(send)

    # ------------------------------------------------------------------ converter

    async def convert_encoded_id(self, encoded_id: str) -> str:
        """URL 里的 encodedID (DUmRFZmFwcnNCcEZv) → 真 fileID (300000000$xxx)。带缓存。"""
        if encoded_id in self._fileid_cache:
            return self._fileid_cache[encoded_id]
        url = self.BASE_URL + self._CONVERTER_ENDPOINT
        params = {"type": 2, "value": encoded_id}
        client = await self._client()

        async def send(headers: dict[str, str]) -> httpx.Response:
            return await client.get(url, headers=headers, params=params)

        data = await self._request_with_failover(send)
        file_id = data["fileID"]
        self._fileid_cache[encoded_id] = file_id
        return file_id

    # ------------------------------------------------------------------ fields

    async def get_fields(
        self, file_id: str, sheet_id: str, *, force_refresh: bool = False
    ) -> list[FieldSchema]:
        key = (file_id, sheet_id)
        if not force_refresh and key in self._fields_cache:
            return self._fields_cache[key]
        data = await self._call_sheet(
            file_id, sheet_id, {"getFields": {"offset": 0, "limit": self._DEFAULT_PAGE_SIZE}}
        )
        raw_fields = data.get("getFields", {}).get("fields", [])
        schemas = [_parse_field(raw) for raw in raw_fields]
        self._fields_cache[key] = schemas
        return schemas

    # ------------------------------------------------------------------ records

    async def get_records(self, file_id: str, sheet_id: str) -> list[Record]:
        fields = await self.get_fields(file_id, sheet_id)
        out: list[Record] = []
        offset = 0
        while True:
            data = await self._call_sheet(
                file_id,
                sheet_id,
                {"getRecords": {"offset": offset, "limit": self._DEFAULT_PAGE_SIZE}},
            )
            block = data.get("getRecords", {})
            for raw in block.get("records", []):
                out.append(_parse_record(raw, fields))
            if not block.get("hasMore"):
                break
            offset = block.get("next", offset + self._DEFAULT_PAGE_SIZE)
        return out

    async def get_record(
        self, file_id: str, sheet_id: str, record_id: str
    ) -> Record | None:
        """腾讯智能表 API 不直接支持按 recordID 拉单条，分页查找。

        对我们的体量（每番剧每集 ~10 条，单表总量百级别）完全够用。
        TODO 若后续 getRecords 支持 filter 参数，改这里。
        """
        fields = await self.get_fields(file_id, sheet_id)
        offset = 0
        while True:
            data = await self._call_sheet(
                file_id,
                sheet_id,
                {"getRecords": {"offset": offset, "limit": self._DEFAULT_PAGE_SIZE}},
            )
            block = data.get("getRecords", {})
            for raw in block.get("records", []):
                if raw.get("recordID") == record_id:
                    return _parse_record(raw, fields)
            if not block.get("hasMore"):
                return None
            offset = block.get("next", offset + self._DEFAULT_PAGE_SIZE)

    async def add_records(
        self, file_id: str, sheet_id: str, rows: list[dict[str, Any]]
    ) -> list[Record]:
        fields = await self.get_fields(file_id, sheet_id)
        by_title = {f.title: f for f in fields}
        wrapped = []
        for row in rows:
            values = {}
            for col, val in row.items():
                field = by_title.get(col)
                if field is None:
                    raise UnknownColumnError(f"列「{col}」不存在于子表 {sheet_id}")
                values[col] = _wrap_value(field, val)
            wrapped.append({"values": values})
        data = await self._call_sheet(
            file_id, sheet_id, {"addRecords": {"records": wrapped}}
        )
        return [_parse_record(r, fields) for r in data.get("addRecords", {}).get("records", [])]

    async def update_record(
        self, file_id: str, sheet_id: str, record_id: str, values: dict[str, Any]
    ) -> Record:
        fields = await self.get_fields(file_id, sheet_id)
        by_title = {f.title: f for f in fields}
        wrapped: dict[str, Any] = {}
        for col, val in values.items():
            field = by_title.get(col)
            if field is None:
                raise UnknownColumnError(f"列「{col}」不存在于子表 {sheet_id}")
            wrapped[col] = _wrap_value(field, val)
        data = await self._call_sheet(
            file_id,
            sheet_id,
            {
                "updateRecords": {
                    "records": [{"recordID": record_id, "values": wrapped}]
                }
            },
        )
        records = data.get("updateRecords", {}).get("records", [])
        if not records:
            raise RecordNotFoundError(f"update returned empty records for {record_id}")
        return _parse_record(records[0], fields)

    async def delete_records(
        self, file_id: str, sheet_id: str, record_ids: list[str]
    ) -> None:
        await self._call_sheet(
            file_id, sheet_id, {"deleteRecords": {"recordIDs": record_ids}}
        )


# ============================================================ schema / value helpers


def _parse_field(raw: dict[str, Any]) -> FieldSchema:
    type_id = raw.get("fieldType")
    type_name = _FIELD_TYPE_MAP.get(type_id, "unknown")
    options: tuple[str, ...] | None = None
    option_id_to_text: dict[str, str] | None = None
    if type_name == "single_select":
        raw_opts = (raw.get("propertySingleSelect") or {}).get("options") or []
        options = tuple(o.get("text", "") for o in raw_opts)
        option_id_to_text = {o["id"]: o.get("text", "") for o in raw_opts if "id" in o}
    return FieldSchema(
        field_id=raw["fieldID"],
        title=raw["fieldTitle"],
        type=type_name,
        options=options,
        option_id_to_text=option_id_to_text,
    )


def _parse_record(raw: dict[str, Any], fields: list[FieldSchema]) -> Record:
    by_title = {f.title: f for f in fields}
    values: dict[str, Any] = {}
    for title, raw_val in (raw.get("values") or {}).items():
        field = by_title.get(title)
        if field is None:
            values[title] = raw_val  # 未知列原样保留
        else:
            values[title] = _unwrap_value(field, raw_val)
    return Record(record_id=raw["recordID"], values=values)


def _wrap_value(field: FieldSchema, value: Any) -> Any:
    """Python 值 → 腾讯智能表期望的 API 形态。"""
    if value is None:
        # 空值用空数组覆盖（文本/单选都接受 []）
        return []
    t = field.type
    if t == "text":
        return [{"type": "text", "text": str(value)}]
    if t == "single_select":
        # 关键坑：必须是数组，裸字符串会被静默丢弃
        return [{"text": str(value)}]
    if t == "datetime":
        if isinstance(value, (int, float)):
            return str(int(value))
        if isinstance(value, datetime):
            return str(int(value.timestamp() * 1000))
        if isinstance(value, date):
            return str(int(datetime(value.year, value.month, value.day).timestamp() * 1000))
        if isinstance(value, str):
            return value  # 假定已经是 unix ms 字符串
        raise StorageError(f"unsupported datetime value: {value!r}")
    if t == "number":
        return value
    # unknown：原样传，让 API 自己决定
    return value


def _unwrap_value(field: FieldSchema, raw: Any) -> Any:
    """API 响应 → Python 值。"""
    if raw is None or raw == []:
        return None
    t = field.type
    if t == "text":
        # raw 形如 [{"type":"text","text":"v"}, ...]
        if isinstance(raw, list):
            return "".join(
                seg.get("text", "") for seg in raw if isinstance(seg, dict)
            )
        return raw
    if t == "single_select":
        # 读响应是 [{"id":..,"style":..,"text":".."}]；update 响应是 ["option_id"]
        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict):
                return first.get("text")
            if isinstance(first, str) and field.option_id_to_text:
                return field.option_id_to_text.get(first)
            return None
        return None
    if t == "datetime":
        try:
            return datetime.fromtimestamp(int(raw) / 1000)
        except (TypeError, ValueError):
            return raw
    if t == "number":
        return raw
    return raw
