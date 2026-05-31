"""storage 层单元测试（纯函数 + D18 多 key 池，均不打真实网络）。"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from nonebot_plugin_subflow.exceptions import StorageError, TokenExpiredError
from nonebot_plugin_subflow.models import FieldSchema, Record
from nonebot_plugin_subflow.storage.tencent_doc import (
    TencentDocStorage,
    _parse_field,
    _parse_record,
    _unwrap_value,
    _wrap_value,
)


# ---------------------------------------------------------- _parse_field


def test_parse_field_text() -> None:
    raw = {"fieldID": "fA1", "fieldTitle": "备注", "fieldType": 1, "propertyText": {}}
    f = _parse_field(raw)
    assert f.field_id == "fA1"
    assert f.title == "备注"
    assert f.type == "text"
    assert f.options is None


def test_parse_field_single_select_captures_options_and_id_map() -> None:
    raw = {
        "fieldID": "fS1",
        "fieldTitle": "进度",
        "fieldType": 17,
        "propertySingleSelect": {
            "options": [
                {"id": "o1", "style": 7, "text": "未分配"},
                {"id": "o2", "style": 13, "text": "已分配"},
                {"id": "o3", "style": 15, "text": "进行中"},
            ]
        },
    }
    f = _parse_field(raw)
    assert f.type == "single_select"
    assert f.options == ("未分配", "已分配", "进行中")
    assert f.option_id_to_text == {"o1": "未分配", "o2": "已分配", "o3": "进行中"}


def test_parse_field_datetime_type_4() -> None:
    raw = {"fieldID": "fD1", "fieldTitle": "完成时间", "fieldType": 4, "propertyDateTime": {}}
    assert _parse_field(raw).type == "datetime"


def test_parse_field_unknown_type_maps_to_unknown() -> None:
    raw = {"fieldID": "fU1", "fieldTitle": "相关流程", "fieldType": 18, "propertyReference": {}}
    f = _parse_field(raw)
    assert f.type == "unknown"
    assert f.options is None


# ---------------------------------------------------------- _wrap_value


def _f(type_: str, **kw: object) -> FieldSchema:
    return FieldSchema(field_id="x", title="t", type=type_, **kw)  # type: ignore[arg-type]


def test_wrap_text_basic() -> None:
    assert _wrap_value(_f("text"), "hello") == [{"type": "text", "text": "hello"}]


def test_wrap_text_coerces_non_str() -> None:
    assert _wrap_value(_f("text"), 123) == [{"type": "text", "text": "123"}]


def test_wrap_single_select_uses_array_form() -> None:
    """D11: 单选必须用 [{'text': '...'}]，裸字符串会被静默丢弃。"""
    assert _wrap_value(_f("single_select"), "翻译") == [{"text": "翻译"}]


def test_wrap_none_returns_empty_array() -> None:
    assert _wrap_value(_f("text"), None) == []
    assert _wrap_value(_f("single_select"), None) == []


def test_wrap_number_passthrough() -> None:
    assert _wrap_value(_f("number"), 42) == 42
    assert _wrap_value(_f("number"), 3.14) == 3.14


def test_wrap_datetime_int_passthrough() -> None:
    assert _wrap_value(_f("datetime"), 1776768420000) == "1776768420000"


def test_wrap_datetime_from_datetime_object() -> None:
    dt = datetime(2026, 5, 26, 14, 0, 0)
    out = _wrap_value(_f("datetime"), dt)
    assert isinstance(out, str)
    # round-trip
    assert datetime.fromtimestamp(int(out) / 1000) == dt


def test_wrap_datetime_raises_on_garbage() -> None:
    with pytest.raises(StorageError):
        _wrap_value(_f("datetime"), object())


def test_wrap_unknown_type_passthrough() -> None:
    sentinel = {"hello": "world"}
    assert _wrap_value(_f("unknown"), sentinel) is sentinel


# ---------------------------------------------------------- _unwrap_value


def test_unwrap_text() -> None:
    raw = [{"type": "text", "text": "hello"}]
    assert _unwrap_value(_f("text"), raw) == "hello"


def test_unwrap_text_concatenates_segments() -> None:
    raw = [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
    assert _unwrap_value(_f("text"), raw) == "foobar"


def test_unwrap_single_select_read_form_dict() -> None:
    """从 getRecords 拿到的形态 — dict 含 id/style/text。"""
    raw = [{"id": "o1", "style": 13, "text": "翻译"}]
    assert _unwrap_value(_f("single_select"), raw) == "翻译"


def test_unwrap_single_select_update_form_option_id() -> None:
    """update 响应只回 option_id；需借助 option_id_to_text 反查。"""
    field = _f("single_select", option_id_to_text={"oTcNGe": "已完成"})
    assert _unwrap_value(field, ["oTcNGe"]) == "已完成"


def test_unwrap_single_select_unknown_id_returns_none() -> None:
    field = _f("single_select", option_id_to_text={"o1": "x"})
    assert _unwrap_value(field, ["o999"]) is None


def test_unwrap_single_select_empty_array() -> None:
    assert _unwrap_value(_f("single_select"), []) is None


def test_unwrap_datetime_unix_ms_string() -> None:
    raw = "1776768420000"
    out = _unwrap_value(_f("datetime"), raw)
    assert isinstance(out, datetime)
    assert out == datetime.fromtimestamp(1776768420)


def test_unwrap_none_or_empty_returns_none() -> None:
    assert _unwrap_value(_f("text"), None) is None
    assert _unwrap_value(_f("text"), []) is None


# ---------------------------------------------------------- _parse_record


def test_parse_record_normalizes_known_columns_and_keeps_unknown() -> None:
    fields = [
        FieldSchema("f1", "类型", "single_select", options=("翻译",), option_id_to_text={"o1": "翻译"}),
        FieldSchema("f2", "组员", "text"),
        FieldSchema("f3", "完成时间", "datetime"),
    ]
    raw = {
        "recordID": "rABC",
        "values": {
            "类型": [{"id": "o1", "text": "翻译"}],
            "组员": [{"type": "text", "text": "12345"}],
            "完成时间": "1776768420000",
            "未知列": [{"hidden": True}],  # 未知列原样保留
        },
    }
    rec = _parse_record(raw, fields)
    assert rec.record_id == "rABC"
    assert rec.values["类型"] == "翻译"
    assert rec.values["组员"] == "12345"
    assert isinstance(rec.values["完成时间"], datetime)
    assert rec.values["未知列"] == [{"hidden": True}]


def test_record_default_values_dict() -> None:
    rec = Record(record_id="r1")
    assert rec.values == {}


# ---------------------------------------------------------- D18 多 key 轮换池


TWO_KEYS = [
    {"client_id": "c1", "open_id": "o1", "access_token": "t1"},
    {"client_id": "c2", "open_id": "o2", "access_token": "t2"},
]


def _ok(payload: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"ret": 0, "data": payload or {"ok": 1}})


def _err(ret: int) -> httpx.Response:
    return httpx.Response(200, json={"ret": ret, "msg": f"err {ret}"})


def _make_storage(handler, *, keys=TWO_KEYS, **kw) -> TencentDocStorage:
    st = TencentDocStorage(keys=keys, **kw)
    st._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    return st


async def test_pool_round_robin_rotates_open_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Open-Id", ""))
        return _ok()

    st = _make_storage(handler)
    await st._call_sheet("F", "S", {"x": 1})
    await st._call_sheet("F", "S", {"x": 2})
    await st._call_sheet("F", "S", {"x": 3})
    await st.aclose()
    assert seen == ["o1", "o2", "o1"]  # 逐调用轮换


async def test_pool_failover_on_token_error_marks_dead() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # o1 的 token 失效，o2 正常
        if request.headers.get("Open-Id") == "o1":
            return _err(10011)  # token expired
        return _ok()

    st = _make_storage(handler)
    data = await st._call_sheet("F", "S", {"x": 1})
    assert data == {"ok": 1}
    assert st._keys[0].dead is True  # o1 出池
    assert st._keys[1].dead is False
    # 后续调用直接跳过 dead 的 o1
    seen: list[str] = []

    def handler2(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Open-Id", ""))
        return _ok()

    st._http = httpx.AsyncClient(transport=httpx.MockTransport(handler2))  # type: ignore[attr-defined]
    await st._call_sheet("F", "S", {"x": 2})
    await st.aclose()
    assert seen == ["o2"]


async def test_pool_failover_on_configured_rate_limit_cools_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Open-Id") == "o1":
            return _err(9999)  # 配置内的限流码
        return _ok()

    st = _make_storage(handler, rate_limit_rets={9999}, key_cooldown_seconds=60)
    data = await st._call_sheet("F", "S", {"x": 1})
    await st.aclose()
    assert data == {"ok": 1}
    assert st._keys[0].cooldown_until is not None  # o1 进入冷却
    assert st._keys[0].dead is False  # 限流不等于失效


async def test_pool_does_not_failover_on_other_ret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _err(500)  # 非 token、非配置限流

    st = _make_storage(handler, rate_limit_rets={9999})
    with pytest.raises(StorageError):
        await st._call_sheet("F", "S", {"x": 1})
    await st.aclose()
    # 只试了第一把 key，没有转移
    assert st._keys[0].calls == 1
    assert st._keys[1].calls == 0


async def test_pool_network_error_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    st = _make_storage(handler)
    with pytest.raises(httpx.ConnectError):
        await st._call_sheet("F", "S", {"x": 1})
    await st.aclose()
    # 网络异常不换 key 重试（防重复落库）
    assert st._keys[0].calls == 1
    assert st._keys[1].calls == 0


async def test_pool_all_keys_dead_raises_token_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _err(10011)  # 两把都 token 失效

    st = _make_storage(handler)
    with pytest.raises(TokenExpiredError):
        await st._call_sheet("F", "S", {"x": 1})
    await st.aclose()
    assert all(k.dead for k in st._keys)


async def test_legacy_single_key_constructor_still_works() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Open-Id") == "o"
        return _ok()

    st = TencentDocStorage(client_id="c", open_id="o", access_token="t")
    st._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    data = await st._call_sheet("F", "S", {"x": 1})
    await st.aclose()
    assert data == {"ok": 1}
    assert len(st._keys) == 1
