"""config.py 单元测试 — D18 凭据池解析（resolved_keys）。"""

from __future__ import annotations

from nonebot_plugin_subflow.config import Config, TencentDocKey


def test_resolved_keys_empty_when_nothing_configured() -> None:
    cfg = Config()
    assert cfg.resolved_keys == []


def test_resolved_keys_falls_back_to_trio() -> None:
    cfg = Config(
        tencent_doc_client_id="c",
        tencent_doc_open_id="o",
        tencent_doc_access_token="t",
    )
    keys = cfg.resolved_keys
    assert len(keys) == 1
    assert keys[0] == TencentDocKey(client_id="c", open_id="o", access_token="t")


def test_resolved_keys_uses_list_when_present() -> None:
    cfg = Config(
        subflow_tencent_doc_keys=[
            {"client_id": "c1", "open_id": "o1", "access_token": "t1"},
            {"client_id": "c2", "open_id": "o2", "access_token": "t2"},
        ],
        # 三元组也填了，但列表非空时应被忽略
        tencent_doc_client_id="cX",
        tencent_doc_open_id="oX",
        tencent_doc_access_token="tX",
    )
    keys = cfg.resolved_keys
    assert [k.open_id for k in keys] == ["o1", "o2"]


def test_resolved_keys_incomplete_trio_is_empty() -> None:
    # 只填了 client_id，缺另外两个 → 不算有效单 key
    cfg = Config(tencent_doc_client_id="c")
    assert cfg.resolved_keys == []
