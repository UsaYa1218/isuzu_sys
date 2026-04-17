from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..config import settings
from ..schemas import ExtractedField, ExtractionResult, OCRLine, VoucherItemDraft
from .llm import normalize_with_ollama


FIELD_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "invoice": {
        "issue_date": {"labels": ["発行日", "請求日", "日付"], "type": "date", "required": True},
        "due_date": {"labels": ["支払期限", "支払日", "お支払期限"], "type": "date", "required": False},
        "document_number": {"labels": ["請求書番号", "請求No", "No.", "No"], "type": "text", "required": True},
        "vendor_name": {"labels": ["発行者", "請求元", "差出人", "株式会社"], "type": "text", "required": True},
        "customer_name": {"labels": ["請求先", "御中", "宛先"], "type": "text", "required": False},
        "currency": {"labels": ["通貨", "Currency"], "type": "currency", "required": False},
        "subtotal": {"labels": ["小計", "税抜金額"], "type": "money", "required": False},
        "tax": {"labels": ["消費税", "税額"], "type": "money", "required": False},
        "discount": {"labels": ["値引き", "割引"], "type": "money", "required": False},
        "grand_total": {"labels": ["合計", "ご請求額", "請求金額"], "type": "money", "required": True},
        "notes": {"labels": ["備考", "摘要"], "type": "text", "required": False},
    },
    "delivery": {
        "issue_date": {"labels": ["納品日", "日付"], "type": "date", "required": True},
        "document_number": {"labels": ["納品書番号", "伝票番号", "No."], "type": "text", "required": True},
        "vendor_name": {"labels": ["出荷元", "送付元", "発行者"], "type": "text", "required": True},
        "customer_name": {"labels": ["納品先", "宛先"], "type": "text", "required": True},
        "notes": {"labels": ["備考"], "type": "text", "required": False},
        "grand_total": {"labels": ["合計"], "type": "money", "required": False},
    },
    "journal": {
        "issue_date": {"labels": ["伝票日付", "日付"], "type": "date", "required": True},
        "document_number": {"labels": ["伝票番号", "No."], "type": "text", "required": True},
        "vendor_name": {"labels": ["起票者", "部門"], "type": "text", "required": False},
        "notes": {"labels": ["摘要", "備考"], "type": "text", "required": True},
        "grand_total": {"labels": ["借方合計", "貸方合計"], "type": "money", "required": False},
    },
}


ITEM_HEADERS = {
    "invoice": ["品名", "摘要", "内容", "数量", "単価", "金額"],
    "delivery": ["品名", "内容", "数量", "単位", "備考"],
    "journal": ["摘要", "借方", "貸方", "金額"],
}


DATE_PATTERNS = [
    re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _line_text_after_label(text: str, label: str) -> str | None:
    patterns = [
        rf"{re.escape(label)}\s*[:：]?\s*(.+)",
        rf"{re.escape(label)}\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = _normalize_space(match.group(1))
            if value and value != label:
                return value
    return None


def _parse_date(text: str | None) -> str | None:
    if not text:
        return None
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _parse_money(text: str | None) -> float | None:
    if not text:
        return None
    candidate = text.replace("¥", "").replace("￥", "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", candidate)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    candidate = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", candidate)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_currency(text: str | None) -> str | None:
    if not text:
        return None
    upper = text.upper()
    if "JPY" in upper or "円" in text or "¥" in text or "￥" in text:
        return "JPY"
    match = re.search(r"\b[A-Z]{3}\b", upper)
    return match.group(0) if match else None


def _distance_score(label: OCRLine, candidate: OCRLine) -> float:
    horizontal = candidate.left - label.left
    vertical = abs(candidate.center_y - label.center_y)
    penalty = max(0.0, vertical * 0.4) + max(0.0, -horizontal * 2.0)
    return candidate.confidence - (penalty / 100.0)


def _select_nearby_value(label_line: OCRLine, lines: list[OCRLine]) -> OCRLine | None:
    candidates: list[tuple[float, OCRLine]] = []
    for line in lines:
        if line.page != label_line.page or line.text == label_line.text:
            continue
        same_row = abs(line.center_y - label_line.center_y) < 24 and line.left > label_line.left
        below = 0 < (line.top - label_line.top) < 120 and abs(line.left - label_line.left) < 220
        if same_row or below:
            candidates.append((_distance_score(label_line, line), line))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _parse_by_type(value_type: str, text: str | None) -> str | float | None:
    if value_type == "date":
        return _parse_date(text)
    if value_type == "money":
        return _parse_money(text)
    if value_type == "number":
        return _parse_number(text)
    if value_type == "currency":
        return _parse_currency(text) or "JPY"
    if text is None:
        return None
    return _normalize_space(text)


def _find_field(lines: list[OCRLine], key: str, spec: dict[str, Any]) -> ExtractedField:
    label_matches: list[tuple[float, OCRLine, str | None]] = []
    for line in lines:
        normalized = _normalize_space(line.text)
        for label in spec["labels"]:
            if label.lower() not in normalized.lower():
                continue
            inline_value = _line_text_after_label(normalized, label)
            label_matches.append((line.confidence, line, inline_value))

    if not label_matches:
        return ExtractedField(key=key, value=None, raw_text=None, confidence=0.0, bbox=None, needs_review=spec.get("required", False))

    label_matches.sort(key=lambda item: item[0], reverse=True)
    _, label_line, inline_value = label_matches[0]
    raw_text = inline_value
    value_line = None
    if raw_text is None:
        value_line = _select_nearby_value(label_line, lines)
        raw_text = value_line.text if value_line else None

    parsed = _parse_by_type(spec["type"], raw_text)
    confidence = max(label_line.confidence, value_line.confidence if value_line else 0.0)
    needs_review = parsed is None or confidence < settings.ocr_confidence_threshold

    return ExtractedField(
        key=key,
        value=parsed,
        raw_text=raw_text,
        confidence=round(confidence, 3),
        bbox=(value_line.bbox if value_line else label_line.bbox),
        needs_review=needs_review,
    )


def _group_lines_by_row(lines: list[OCRLine], tolerance: float = 18.0) -> list[list[OCRLine]]:
    rows: list[list[OCRLine]] = []
    for line in lines:
        placed = False
        for row in rows:
            if abs(row[0].center_y - line.center_y) <= tolerance and row[0].page == line.page:
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda entry: entry.left)
    rows.sort(key=lambda row: (row[0].page, row[0].center_y))
    return rows


def _detect_item_rows(lines: list[OCRLine], voucher_type: str) -> list[VoucherItemDraft]:
    headers = ITEM_HEADERS.get(voucher_type, ITEM_HEADERS["invoice"])
    rows = _group_lines_by_row(lines)
    header_index = -1
    header_row: list[OCRLine] = []
    for index, row in enumerate(rows):
        row_text = " ".join(cell.text for cell in row)
        matched = sum(1 for header in headers if header in row_text)
        if matched >= 2:
            header_index = index
            header_row = row
            break

    if header_index < 0 or not header_row:
        return []

    column_positions: dict[str, float] = {}
    for cell in header_row:
        for header in headers:
            if header in cell.text:
                column_positions[header] = cell.center_x

    items: list[VoucherItemDraft] = []
    stop_keywords = {"小計", "合計", "消費税", "税額", "請求金額", "備考"}

    for row in rows[header_index + 1 :]:
        row_text = " ".join(cell.text for cell in row).strip()
        if not row_text:
            continue
        if any(keyword in row_text for keyword in stop_keywords):
            break
        if len(row_text) <= 1:
            continue

        mapped: dict[str, OCRLine] = {}
        for cell in row:
            nearest_header = min(
                column_positions.items(),
                key=lambda item: abs(cell.center_x - item[1]),
                default=(None, None),
            )
            if nearest_header[0] is None:
                continue
            mapped.setdefault(nearest_header[0], cell)

        description = mapped.get("品名") or mapped.get("摘要") or mapped.get("内容")
        quantity = mapped.get("数量")
        unit = mapped.get("単位")
        unit_price = mapped.get("単価") or mapped.get("借方") or mapped.get("貸方")
        amount = mapped.get("金額")

        item = VoucherItemDraft(
            description=_normalize_space(description.text) if description else row_text,
            quantity=_parse_number(quantity.text) if quantity else None,
            unit=_normalize_space(unit.text) if unit else None,
            unit_price=_parse_money(unit_price.text) if unit_price else None,
            amount=_parse_money(amount.text) if amount else None,
            confidence=round(sum(cell.confidence for cell in row) / len(row), 3),
            needs_review=any(cell.confidence < settings.ocr_confidence_threshold for cell in row),
        )

        if not item.description and item.amount is None:
            continue
        items.append(item)

    return items


def _draft_for_llm(fields: dict[str, ExtractedField], items: list[VoucherItemDraft]) -> dict[str, Any]:
    return {
        "fields": {key: value.value for key, value in fields.items()},
        "items": [asdict(item) for item in items],
    }


def _value_supported_by_ocr(value: Any, raw_text: str) -> bool:
    if value is None:
        return True
    if isinstance(value, (int, float)):
        compact = str(value).replace(".0", "")
        return compact in raw_text.replace(",", "")
    normalized = re.sub(r"[\s:/-]", "", str(value))
    haystack = re.sub(r"[\s:/-]", "", raw_text)
    return normalized in haystack


def _merge_with_llm(
    voucher_type: str,
    raw_text: str,
    fields: dict[str, ExtractedField],
    items: list[VoucherItemDraft],
) -> tuple[dict[str, ExtractedField], list[VoucherItemDraft], list[str], bool]:
    llm_result = normalize_with_ollama(voucher_type, raw_text, _draft_for_llm(fields, items))
    if not llm_result:
        return fields, items, [], False

    warnings: list[str] = list(llm_result.get("warnings") or [])
    llm_fields = llm_result.get("fields") or {}
    for key, current in fields.items():
        candidate = llm_fields.get(key)
        if candidate is None:
            continue
        if not _value_supported_by_ocr(candidate, raw_text):
            warnings.append(f"LLM candidate for {key} was ignored because it was not supported by OCR text.")
            continue
        if current.value in (None, "", 0) or current.needs_review:
            current.value = candidate
            current.raw_text = str(candidate)
            current.source = "llm"
            current.needs_review = current.confidence < settings.ocr_confidence_threshold

    llm_items = llm_result.get("items") or []
    if llm_items and not items:
        merged_items: list[VoucherItemDraft] = []
        for row in llm_items:
            if not any(value not in (None, "") for value in row.values()):
                continue
            merged_items.append(
                VoucherItemDraft(
                    description=str(row.get("description") or ""),
                    quantity=_parse_number(str(row.get("quantity"))) if row.get("quantity") is not None else None,
                    unit=row.get("unit"),
                    unit_price=_parse_money(str(row.get("unit_price"))) if row.get("unit_price") is not None else None,
                    amount=_parse_money(str(row.get("amount"))) if row.get("amount") is not None else None,
                    tax_rate=_parse_number(str(row.get("tax_rate"))) if row.get("tax_rate") is not None else None,
                    confidence=0.6,
                    needs_review=True,
                )
            )
        items = merged_items or items

    return fields, items, warnings, True


def extract_document(voucher_type: str, lines: list[OCRLine]) -> ExtractionResult:
    specs = FIELD_SPECS.get(voucher_type, FIELD_SPECS["invoice"])
    raw_text = "\n".join(line.text for line in lines)
    fields = {key: _find_field(lines, key, spec) for key, spec in specs.items()}
    items = _detect_item_rows(lines, voucher_type)
    fields, items, llm_warnings, llm_used = _merge_with_llm(voucher_type, raw_text, fields, items)

    warnings: list[str] = []
    for key, field in fields.items():
        if field.value is None and specs.get(key, {}).get("required", False):
            warnings.append(f"必須項目 {key} を抽出できませんでした。")
        if field.confidence < settings.ocr_confidence_threshold:
            warnings.append(f"{key} の OCR 信頼度が閾値未満です。")

    return ExtractionResult(
        voucher_type=voucher_type,
        fields=fields,
        items=items,
        warnings=warnings + llm_warnings,
        raw_text=raw_text,
        ocr_lines=lines,
        llm_used=llm_used,
    )
