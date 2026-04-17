from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..config import settings
from ..schemas import ExtractedField, ExtractedTable, ExtractionResult, OCRLine, VoucherItemDraft
from .llm import normalize_with_ollama, reconstruct_tables_with_ollama


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


TABLE_HEADER_ALIASES = {
    "description": ["品名", "摘要", "内容", "車種", "型式", "登録番号", "型式・登録番号", "車台番号", "車輌名称", "車両名称", "車両状態", "特記事項", "その他", "model", "vin", "現在地", "全長", "全幅", "全高", "重量"],
    "quantity": ["数量", "台数"],
    "unit": ["単位"],
    "unit_price": ["単価", "借方", "貸方"],
    "amount": ["金額", "合計", "輸送費"],
    "serial": ["no", "no.", "ｎｏ", "番号", "オーダーno.", "オーダ-no.", "オーダno."],
}


DATE_PATTERNS = [
    re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _normalize_header_text(text: str) -> str:
    return _normalize_space(text).lower().replace("．", ".")


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


def _find_table_column(headers: list[str], aliases: list[str]) -> int | None:
    normalized_headers = [_normalize_header_text(header) for header in headers]
    normalized_aliases = [_normalize_header_text(alias) for alias in aliases]
    for index, header in enumerate(normalized_headers):
        if any(alias and alias in header for alias in normalized_aliases):
            return index
    return None


def _row_cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    value = _normalize_space(row[index])
    return "" if value in {"-", "ー", "－"} else value


def _table_contains_item_candidates(table: ExtractedTable) -> bool:
    headers = table.headers
    if not headers:
        return False
    normalized_headers = [_normalize_header_text(header) for header in headers]
    normalized_aliases = [
        _normalize_header_text(item)
        for aliases in TABLE_HEADER_ALIASES.values()
        for item in aliases
    ]
    matched_columns = 0
    for header in normalized_headers:
        if any(alias and alias in header for alias in normalized_aliases):
            matched_columns += 1
    vehicle_spec = any("vin" in header for header in normalized_headers) and any("現在地" in header for header in headers)
    return matched_columns >= 2 or vehicle_spec


def _detect_item_rows_from_tables(tables: list[ExtractedTable]) -> list[VoucherItemDraft]:
    items: list[VoucherItemDraft] = []
    for table in tables:
        if not _table_contains_item_candidates(table):
            continue

        serial_index = _find_table_column(table.headers, TABLE_HEADER_ALIASES["serial"])
        description_indexes = [
            index
            for index in (
                _find_table_column(table.headers, ["品名", "摘要", "内容"]),
                _find_table_column(table.headers, ["車種"]),
                _find_table_column(table.headers, ["型式", "登録番号", "型式・登録番号"]),
                _find_table_column(table.headers, ["車台番号"]),
                _find_table_column(table.headers, ["車輌名称", "車両名称"]),
                _find_table_column(table.headers, ["車両状態"]),
                _find_table_column(table.headers, ["特記事項", "その他"]),
                _find_table_column(table.headers, ["MODEL"]),
                _find_table_column(table.headers, ["Vin"]),
                _find_table_column(table.headers, ["現在地"]),
                _find_table_column(table.headers, ["全長"]),
                _find_table_column(table.headers, ["全幅"]),
                _find_table_column(table.headers, ["全高"]),
                _find_table_column(table.headers, ["重量"]),
            )
            if index is not None
        ]
        quantity_index = _find_table_column(table.headers, TABLE_HEADER_ALIASES["quantity"])
        unit_index = _find_table_column(table.headers, TABLE_HEADER_ALIASES["unit"])
        unit_price_index = _find_table_column(table.headers, TABLE_HEADER_ALIASES["unit_price"])
        amount_index = _find_table_column(table.headers, TABLE_HEADER_ALIASES["amount"])

        for row in table.rows:
            serial_value = _row_cell(row, serial_index)
            if serial_value and any(keyword in serial_value for keyword in ("備考",)):
                continue

            description_parts = [_row_cell(row, index) for index in description_indexes]
            description_parts = [part for part in description_parts if part]
            description = " / ".join(dict.fromkeys(description_parts))
            quantity = _parse_number(_row_cell(row, quantity_index))
            unit = _row_cell(row, unit_index) or None
            unit_price = _parse_money(_row_cell(row, unit_price_index))
            amount = _parse_money(_row_cell(row, amount_index))

            meaningful_cells = [cell for cell in row if _normalize_space(cell)]
            if not meaningful_cells:
                continue
            if description == "" and amount is None and quantity is None:
                continue
            if serial_value and len(meaningful_cells) == 1:
                continue

            items.append(
                VoucherItemDraft(
                    description=description or " / ".join(meaningful_cells),
                    quantity=quantity,
                    unit=unit,
                    unit_price=unit_price,
                    amount=amount,
                    confidence=0.7,
                    needs_review=True,
                )
            )
    return items


def _draft_for_llm(fields: dict[str, ExtractedField], items: list[VoucherItemDraft]) -> dict[str, Any]:
    return {
        "fields": {key: value.value for key, value in fields.items()},
        "items": [asdict(item) for item in items],
    }


def _tables_for_llm(tables: list[ExtractedTable]) -> list[dict[str, Any]]:
    return [
        {
            "title": table.title,
            "headers": table.headers,
            "rows": table.rows,
        }
        for table in tables
    ]


def _text_supported_by_ocr(value: str, raw_text: str) -> bool:
    if not value:
        return True
    normalized = re.sub(r"[\s:/-]", "", value)
    haystack = re.sub(r"[\s:/-]", "", raw_text)
    if normalized and normalized in haystack:
        return True
    digits = re.sub(r"\D", "", value)
    return bool(digits) and len(digits) >= 4 and digits in re.sub(r"\D", "", raw_text)


def _table_is_low_quality(table: ExtractedTable) -> bool:
    normalized_headers = [_normalize_space(header) for header in table.headers]
    if not normalized_headers:
        return False
    empty_headers = sum(1 for header in normalized_headers if not header)
    oversized_headers = sum(1 for header in normalized_headers if len(header) >= 24)
    return empty_headers >= max(2, len(normalized_headers) // 3) or oversized_headers >= max(1, len(normalized_headers) // 2)


def _tables_need_llm_reconstruction(tables: list[ExtractedTable]) -> bool:
    return any(_table_is_low_quality(table) for table in tables)


def _table_quality_score(tables: list[ExtractedTable]) -> int:
    score = 0
    for table in tables:
        normalized_headers = [_normalize_space(header) for header in table.headers]
        score += sum(2 for header in normalized_headers if header and len(header) < 24)
        score -= sum(2 for header in normalized_headers if not header)
        score -= sum(1 for header in normalized_headers if len(header) >= 24)
        score += sum(1 for row in table.rows if any(_normalize_space(cell) for cell in row))
    return score


def _normalize_llm_items(llm_items: list[dict[str, Any]], raw_text: str) -> list[VoucherItemDraft]:
    normalized_items: list[VoucherItemDraft] = []
    for row in llm_items:
        if not isinstance(row, dict):
            continue
        description = _normalize_space(str(row.get("description") or ""))
        if description and not _text_supported_by_ocr(description, raw_text):
            description = ""
        quantity = _parse_number(str(row.get("quantity"))) if row.get("quantity") is not None else None
        unit = _normalize_space(str(row.get("unit") or "")) or None
        unit_price = _parse_money(str(row.get("unit_price"))) if row.get("unit_price") is not None else None
        amount = _parse_money(str(row.get("amount"))) if row.get("amount") is not None else None
        tax_rate = _parse_number(str(row.get("tax_rate"))) if row.get("tax_rate") is not None else None
        if not any([description, quantity is not None, unit_price is not None, amount is not None]):
            continue
        normalized_items.append(
            VoucherItemDraft(
                description=description,
                quantity=quantity,
                unit=unit,
                unit_price=unit_price,
                amount=amount,
                tax_rate=tax_rate,
                confidence=0.55,
                needs_review=True,
            )
        )
    return normalized_items


def _normalize_llm_tables(llm_tables: list[dict[str, Any]], raw_text: str, fallback_page: int) -> list[ExtractedTable]:
    normalized_tables: list[ExtractedTable] = []
    for index, table_data in enumerate(llm_tables, start=1):
        if not isinstance(table_data, dict):
            continue
        headers = [_normalize_space(str(header)) for header in table_data.get("headers") or []]
        headers = [header for header in headers if header]
        if not headers:
            continue
        rows: list[list[str]] = []
        for raw_row in table_data.get("rows") or []:
            if not isinstance(raw_row, list):
                continue
            cells = [_normalize_space(str(cell)) for cell in raw_row[: len(headers)]]
            cells += [""] * (len(headers) - len(cells))
            filtered_cells = [cell if _text_supported_by_ocr(cell, raw_text) else "" for cell in cells]
            if any(filtered_cells):
                rows.append(filtered_cells)
        if not rows:
            continue
        normalized_tables.append(
            ExtractedTable(
                page=fallback_page,
                table_index=index,
                bbox=[0.0, 0.0, 0.0, 0.0],
                title=_normalize_space(str(table_data.get("title") or f"LLM再構成表{index}")),
                headers=headers,
                rows=rows,
            )
        )
    return normalized_tables


def _preserve_empty_cells_from_original(
    original_tables: list[ExtractedTable],
    reconstructed_tables: list[ExtractedTable],
) -> list[ExtractedTable]:
    if len(original_tables) != len(reconstructed_tables):
        return reconstructed_tables

    preserved_tables: list[ExtractedTable] = []
    for original, reconstructed in zip(original_tables, reconstructed_tables, strict=False):
        if len(original.headers) != len(reconstructed.headers):
            return reconstructed_tables
        if len(original.rows) != len(reconstructed.rows):
            return reconstructed_tables

        rows: list[list[str]] = []
        for original_row, reconstructed_row in zip(original.rows, reconstructed.rows, strict=False):
            if len(original_row) != len(reconstructed_row):
                return reconstructed_tables
            row: list[str] = []
            for original_cell, reconstructed_cell in zip(original_row, reconstructed_row, strict=False):
                row.append("" if not _normalize_space(original_cell) else reconstructed_cell)
            rows.append(row)

        preserved_tables.append(
            ExtractedTable(
                page=reconstructed.page,
                table_index=reconstructed.table_index,
                bbox=reconstructed.bbox,
                title=reconstructed.title,
                headers=reconstructed.headers,
                rows=rows,
            )
        )

    return preserved_tables


def _combine_llm_statuses(statuses: list[str]) -> str:
    if "applied" in statuses:
        return "applied"
    if "failed" in statuses:
        return "failed"
    return "unused"


def _ocr_supports_date(value: str, raw_text: str) -> bool:
    candidate = _parse_date(value)
    if not candidate:
        return False

    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(raw_text):
            try:
                year, month, day = (int(part) for part in match.groups())
                normalized = datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                continue
            if normalized == candidate:
                return True
    return False


def _value_supported_by_ocr(key: str, value: Any, raw_text: str) -> bool:
    if value is None:
        return True
    if key in {"issue_date", "due_date"} and isinstance(value, str):
        return _ocr_supports_date(value, raw_text)
    if key == "currency" and value == "JPY":
        upper = raw_text.upper()
        if any(code in upper for code in ("USD", "EUR", "CNY", "GBP")):
            return False
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
) -> tuple[dict[str, ExtractedField], list[VoucherItemDraft], list[str], bool, str, list[str]]:
    llm_result, llm_error, attempted = normalize_with_ollama(voucher_type, raw_text, _draft_for_llm(fields, items))
    if not llm_result:
        status = "failed" if attempted else "unused"
        messages = [llm_error] if llm_error else []
        return fields, items, [], False, status, messages

    warnings: list[str] = list(llm_result.get("warnings") or [])
    llm_fields = llm_result.get("fields") or {}
    for key, current in fields.items():
        candidate = llm_fields.get(key)
        if candidate is None:
            continue
        if not _value_supported_by_ocr(key, candidate, raw_text):
            warnings.append(f"LLM candidate for {key} was ignored because it was not supported by OCR text.")
            continue
        if current.value in (None, "", 0) or current.needs_review:
            current.value = candidate
            current.raw_text = str(candidate)
            current.source = "llm"
            current.needs_review = current.confidence < settings.ocr_confidence_threshold

    llm_items = llm_result.get("items") or []
    if llm_items and not items:
        merged_items = _normalize_llm_items(llm_items, raw_text)
        items = merged_items or items

    return fields, items, warnings, True, "applied", []


def _merge_reconstructed_tables_with_llm(
    voucher_type: str,
    raw_text: str,
    tables: list[ExtractedTable],
    items: list[VoucherItemDraft],
) -> tuple[list[ExtractedTable], list[VoucherItemDraft], list[str], bool, str, list[str]]:
    if not tables or not _tables_need_llm_reconstruction(tables):
        return tables, items, [], False, "unused", []

    llm_result, llm_error, attempted = reconstruct_tables_with_ollama(voucher_type, raw_text, _tables_for_llm(tables))
    if not llm_result:
        status = "failed" if attempted else "unused"
        messages = [llm_error] if llm_error else []
        return tables, items, [], False, status, messages

    warnings: list[str] = list(llm_result.get("warnings") or [])
    candidate_tables = _normalize_llm_tables(llm_result.get("tables") or [], raw_text, min((table.page for table in tables), default=1))
    candidate_tables = _preserve_empty_cells_from_original(tables, candidate_tables)
    if candidate_tables and _table_quality_score(candidate_tables) > _table_quality_score(tables):
        tables = candidate_tables
        warnings.append("表構造を LLM で再構成しました。レビューで確認してください。")

    if not items:
        reconstructed_items = _detect_item_rows_from_tables(tables)
        if reconstructed_items:
            items = reconstructed_items
        else:
            llm_items = _normalize_llm_items(llm_result.get("items") or [], raw_text)
            items = llm_items or items

    return tables, items, warnings, True, "applied", []


def extract_document(voucher_type: str, lines: list[OCRLine], tables: list[ExtractedTable] | None = None) -> ExtractionResult:
    specs = FIELD_SPECS.get(voucher_type, FIELD_SPECS["invoice"])
    raw_text = "\n".join(line.text for line in lines)
    tables = tables or []
    fields = {key: _find_field(lines, key, spec) for key, spec in specs.items()}
    items = _detect_item_rows(lines, voucher_type)
    if not items and tables:
        items = _detect_item_rows_from_tables(tables)
    fields, items, llm_warnings, llm_used, llm_status_1, llm_messages_1 = _merge_with_llm(voucher_type, raw_text, fields, items)
    tables, items, table_llm_warnings, table_llm_used, llm_status_2, llm_messages_2 = _merge_reconstructed_tables_with_llm(voucher_type, raw_text, tables, items)
    llm_status = _combine_llm_statuses([llm_status_1, llm_status_2])
    llm_messages = [message for message in [*llm_messages_1, *llm_messages_2] if message]

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
        warnings=warnings + llm_warnings + table_llm_warnings,
        raw_text=raw_text,
        ocr_lines=lines,
        tables=tables,
        llm_used=llm_used or table_llm_used,
        llm_status=llm_status,
        llm_messages=llm_messages,
    )
