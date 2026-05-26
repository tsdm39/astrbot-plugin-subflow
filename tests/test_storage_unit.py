"""storage 层纯函数单元测试（不打网络）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from nonebot_plugin_subflow.exceptions import StorageError
from nonebot_plugin_subflow.models import FieldSchema, Record
from nonebot_plugin_subflow.storage.tencent_doc import (
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
