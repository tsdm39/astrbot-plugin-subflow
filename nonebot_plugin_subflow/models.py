"""Storage 层使用的领域模型。保持纯数据，不依赖任何后端。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldSchema:
    """一列的元信息。后端无关。"""

    field_id: str
    title: str
    type: str  # "text" | "single_select" | "datetime" | "number" | "unknown"
    options: tuple[str, ...] | None = None  # 仅 single_select 有值（option text 列表）
    # 仅 single_select：option_id → option text 的映射。用于 update 响应（API 只回 option_id）
    option_id_to_text: dict[str, str] | None = None


@dataclass
class Record:
    """一行数据。values 用列标题作为 key，已归一化为 Python 类型。"""

    record_id: str
    values: dict[str, Any] = field(default_factory=dict)
