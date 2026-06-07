from __future__ import annotations

from typing import Any

from ..config import settings
from ..schemas import ExtractionResult


REQUIRED_FIELDS_BY_TYPE = {
    "invoice": {"issue_date", "vendor_name", "grand_total"},
    "delivery": {"issue_date", "vendor_name", "customer_name"},
    "journal": {"issue_date", "notes"},
}


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_extraction(result: ExtractionResult) -> dict[str, Any]:
    warnings = list(result.warnings)
    required_fields = REQUIRED_FIELDS_BY_TYPE.get(result.voucher_type, REQUIRED_FIELDS_BY_TYPE["invoice"])
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

    review_fields = [
        field
        for key, field in result.fields.items()
        if key in required_fields or key != "notes"
    ]
    needs_review = any(field.needs_review for field in review_fields) or any(
        item.needs_review or item.confidence < settings.ocr_confidence_threshold for item in result.items
    )
    needs_review = needs_review or bool(warnings)
    assessed_confidences = [
        field.confidence
        for key, field in result.fields.items()
        if field.value not in (None, "") and (key in required_fields or key != "notes")
    ]
    assessed_confidences.extend(
        item.confidence
        for item in result.items
        if any((item.description, item.quantity, item.unit, item.unit_price, item.amount, item.tax_rate))
    )
    confidence_score = round(min(assessed_confidences), 3) if assessed_confidences else None
    low_confidence = confidence_score is not None and confidence_score < settings.ocr_confidence_threshold
    manual_confirmation_required = needs_review or low_confidence
    status = "REVIEW_REQUIRED" if manual_confirmation_required else "READY_FOR_APPROVAL"

    return {
        "status": status,
        "needs_review": manual_confirmation_required,
        "warnings": warnings,
        "issues_detected": needs_review,
        "confidence_score": confidence_score,
        "confidence_threshold": settings.ocr_confidence_threshold,
        "low_confidence": low_confidence,
        "manual_confirmation_required": manual_confirmation_required,
        "manual_confirmation_completed": not manual_confirmation_required,
    }
