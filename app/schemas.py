from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class OCRLine:
    text: str
    confidence: float
    bbox: list[list[float]]
    page: int

    @property
    def center_x(self) -> float:
        return sum(point[0] for point in self.bbox) / len(self.bbox)

    @property
    def center_y(self) -> float:
        return sum(point[1] for point in self.bbox) / len(self.bbox)

    @property
    def left(self) -> float:
        return min(point[0] for point in self.bbox)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.bbox)


@dataclass(slots=True)
class ExtractedField:
    key: str
    value: str | float | None
    raw_text: str | None
    confidence: float
    bbox: list[list[float]] | None
    needs_review: bool
    source: str = "heuristic"


@dataclass(slots=True)
class VoucherItemDraft:
    description: str = ""
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    amount: float | None = None
    tax_rate: float | None = None
    confidence: float = 0.0
    needs_review: bool = False


@dataclass(slots=True)
class ExtractedTable:
    page: int
    table_index: int
    bbox: list[float]
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    voucher_type: str
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    items: list[VoucherItemDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""
    ocr_lines: list[OCRLine] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    llm_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "voucher_type": self.voucher_type,
            "fields": {key: asdict(value) for key, value in self.fields.items()},
            "items": [asdict(item) for item in self.items],
            "warnings": self.warnings,
            "raw_text": self.raw_text,
            "ocr_lines": [asdict(line) for line in self.ocr_lines],
            "tables": [asdict(table) for table in self.tables],
            "llm_used": self.llm_used,
        }
