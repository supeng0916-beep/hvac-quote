"""报价核心的数据模型：证据、工程量、未计入项、报价结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Evidence:
    """一条可回查的来源：图元、文字、规则或人工确认。"""

    kind: str
    handle: str
    layer: str | None = None
    text: str | None = None
    note: str | None = None


@dataclass
class QuantityItem:
    """工程量清单的一项。quantity=None 表示未知，绝不以 0 代替。"""

    item_id: str
    category: str
    spec: str
    unit: str
    quantity: Decimal | None
    status: str = "known"
    template_row: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    derived_from: str | None = None
    note: str | None = None
    # 项目价补项使用；模板行计价时保持 None。
    unit_prices: tuple[Decimal, Decimal] | None = None
    price_source: str | None = None
    display_name: str | None = None
    display_spec: str | None = None


@dataclass
class OmittedItem:
    """明确未计入的范围；数量保持 None，导出时必须可见。"""

    subject: str
    reason: str
    quantity: None = None
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class TakeoffResult:
    source_sha256: str
    drawing_path: str
    items: list[QuantityItem]
    omitted: list[OmittedItem]
    issues: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
