from __future__ import annotations

from typing import Any

from ..config import settings
from ..schemas import ExtractionResult


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_extraction(result: ExtractionResult) -> dict[str, Any]:
    warnings = list(result.warnings)
    field_values = {key: field.value for key, field in result.fields.items()}
    subtotal = _to_float(field_values.get("subtotal"))
    tax = _to_float(field_values.get("tax"))
    discount = _to_float(field_values.get("discount")) or 0.0
    grand_total = _to_float(field_values.get("grand_total"))

    if result.items:
        for index, item in enumerate(result.items, start=1):
            if item.quantity is not None and item.unit_price is not None:
                expected = round(item.quantity * item.unit_price, 2)
                if item.amount is not None and abs(expected - item.amount) > 1:
                    warnings.append(f"明細 {index} の amount != quantity * unit_price です。")
                    item.needs_review = True

    if subtotal is not None and tax is not None and grand_total is not None:
        expected_total = round(subtotal + tax - discount, 2)
        if abs(expected_total - grand_total) > 1:
            warnings.append("合計整合に差異があります。")

    if result.fields.get("currency") and result.fields["currency"].value is None:
        result.fields["currency"].value = "JPY"

    needs_review = any(field.needs_review for field in result.fields.values()) or any(
        item.needs_review or item.confidence < settings.ocr_confidence_threshold for item in result.items
    )
    needs_review = needs_review or bool(warnings)
    status = "REVIEW_REQUIRED" if needs_review else "READY_FOR_APPROVAL"

    return {
        "status": status,
        "needs_review": needs_review,
        "warnings": warnings,
    }
