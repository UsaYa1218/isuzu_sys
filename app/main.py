from __future__ import annotations

import json
from mimetypes import guess_type
import re
import shutil
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import (
    append_audit_log,
    fetch_all_vouchers,
    fetch_voucher,
    init_db,
    insert_voucher,
    now_iso,
    replace_transfer_records,
    update_status,
    update_transfer_records,
    update_voucher,
)
from .services.exporter import export_transfer_summary_xlsx, export_voucher_csv_zip, export_voucher_xlsx
from .services.extraction import FIELD_SPECS, extract_document
from .services.llm import extract_transfer_rows_with_ollama
from .services.ocr_pipeline import extract_tables, run_ocr
from .services.validation import validate_extraction
from .schemas import ExtractedField, ExtractionResult, ExtractedTable, TransferRecordDraft, VoucherItemDraft


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(settings.templates_path))
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
init_db()
OCR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-worker")
OCR_FUTURES: set[Future[None]] = set()
SUPPORTED_INPUT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}

VOUCHER_TYPE_LABELS = {
    "invoice": "請求書 / 依頼票",
    "delivery": "納品書",
    "journal": "仕訳伝票",
}

STATUS_LABELS = {
    "OCR_PROCESSING": "OCR処理中",
    "OCR_FAILED": "OCR失敗",
    "REVIEW_REQUIRED": "確認が必要",
    "READY_FOR_APPROVAL": "承認待ち",
    "APPROVED": "承認済み",
    "REJECTED": "差戻し",
    "EXPORTED": "出力済み",
}

FIELD_LABELS = {
    "issue_date": "発行日",
    "due_date": "納期 / 支払期限",
    "document_number": "伝票番号 / 発行番号",
    "vendor_name": "依頼元 / 発行元",
    "customer_name": "納入先 / 取引先",
    "currency": "通貨",
    "subtotal": "小計",
    "tax": "税額",
    "discount": "値引き",
    "grand_total": "合計金額",
    "notes": "備考",
}


def _empty_review_demo_voucher() -> dict[str, Any]:
    fields = {
        key: asdict(
            ExtractedField(
                key=key,
                value=None,
                raw_text=None,
                confidence=0.0,
                bbox=None,
                needs_review=True,
                source="demo",
            )
        )
        for key in FIELD_LABELS
    }
    empty_item = {
        "id": "",
        "description": "",
        "quantity": None,
        "unit": None,
        "unit_price": None,
        "amount": None,
        "tax_rate": None,
        "confidence": 0.0,
        "needs_review": True,
    }
    return {
        "id": "demo_empty_review",
        "type": "invoice",
        "status": "REVIEW_REQUIRED",
        "needs_review": True,
        "source_filename": "中間報告用サンプル（実PDFなし）",
        "source_path": "",
        "issue_date": None,
        "due_date": None,
        "document_number": None,
        "vendor_name": None,
        "customer_name": None,
        "currency": None,
        "subtotal": None,
        "tax": None,
        "discount": None,
        "grand_total": None,
        "confidence": 0.0,
        "notes": None,
        "items": [empty_item],
        "transfer_records": [],
        "document_json": {
            "voucher_type": "invoice",
            "fields": fields,
            "items": [empty_item],
            "warnings": ["中間報告用の空データです。抽出対象項目は暫定のため今後変更されます。"],
            "raw_text": "",
            "ocr_lines": [],
            "tables": [
                {
                    "page": 1,
                    "table_index": 1,
                    "bbox": [],
                    "title": "抽出した表（空データ）",
                    "headers": ["内容", "数量", "単位", "単価", "金額", "税率"],
                    "rows": [["", "", "", "", "", ""]],
                }
            ],
            "context_hints": [],
            "llm_used": False,
            "llm_status": "unused",
            "llm_messages": [],
        },
        "validation_json": {
            "status": "REVIEW_REQUIRED",
            "needs_review": True,
            "warnings": ["中間報告用の空データです。抽出対象項目は暫定のため今後変更されます。"],
        },
        "audit_logs": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _log_ocr(message: str) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[OCR] {timestamp} {message}", flush=True)


def _to_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _serialize_items_for_db(voucher_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        serialized.append(
            {
                "id": item.get("id") or _new_id("item"),
                "voucher_id": voucher_id,
                "line_no": index,
                "description": item.get("description"),
                "quantity": item.get("quantity"),
                "unit": item.get("unit"),
                "unit_price": item.get("unit_price"),
                "amount": item.get("amount"),
                "tax_rate": item.get("tax_rate"),
                "confidence": item.get("confidence", 0.0),
                "needs_review": int(bool(item.get("needs_review"))),
            }
        )
    return serialized


def _validate_transfer_record(record: TransferRecordDraft) -> dict[str, Any]:
    warnings: list[str] = []
    if not record.vehicle_model and not record.vehicle_number:
        warnings.append("車種または車両番号を確認してください。")
    if not record.pickup_location:
        warnings.append("引取場所を確認してください。")
    if not record.delivery_location:
        warnings.append("搬入場所を確認してください。")
    if record.confidence < settings.ocr_confidence_threshold:
        warnings.append("陸送情報の信頼度が低いため確認してください。")
    return {
        "warnings": warnings,
        "needs_review": bool(warnings),
        "confidence": round(record.confidence, 3),
        "confidence_threshold": settings.ocr_confidence_threshold,
        "low_confidence": record.confidence < settings.ocr_confidence_threshold,
    }


def _finalize_validation(
    validation: dict[str, Any],
    transfer_records: list[dict[str, Any]] | None = None,
    *,
    manual_confirmation_completed: bool = False,
) -> dict[str, Any]:
    records = transfer_records or []
    transfer_review_count = sum(1 for record in records if record.get("needs_review"))
    transfer_scores = [
        _coerce_transfer_confidence(record.get("confidence"))
        for record in records
        if record.get("confidence") is not None
    ]
    transfer_confidence_score = round(min(transfer_scores), 3) if transfer_scores else None
    confidence_scores = [
        score
        for score in (validation.get("confidence_score"), transfer_confidence_score)
        if score is not None
    ]
    confidence_score = round(min(confidence_scores), 3) if confidence_scores else None
    threshold = settings.ocr_confidence_threshold
    low_confidence = confidence_score is not None and confidence_score < threshold
    issues_detected = bool(validation.get("issues_detected", validation.get("needs_review"))) or transfer_review_count > 0
    required = issues_detected or low_confidence
    completed = bool(manual_confirmation_completed) if required else True

    validation.update(
        {
            "status": "REVIEW_REQUIRED" if required and not completed else "READY_FOR_APPROVAL",
            "needs_review": required and not completed,
            "issues_detected": issues_detected,
            "confidence_score": confidence_score,
            "confidence_threshold": threshold,
            "low_confidence": low_confidence,
            "manual_confirmation_required": required,
            "manual_confirmation_completed": completed,
            "transfer_record_count": len(records),
            "transfer_review_count": transfer_review_count,
            "transfer_confidence_score": transfer_confidence_score,
        }
    )
    return validation


def _requires_uncompleted_confirmation(voucher: dict[str, Any]) -> bool:
    validation = voucher.get("validation_json") or {}
    required = bool(validation.get("manual_confirmation_required", voucher.get("needs_review")))
    completed = bool(validation.get("manual_confirmation_completed", not required))
    return required and not completed


def _require_completed_confirmation(voucher: dict[str, Any], operation: str) -> None:
    if _requires_uncompleted_confirmation(voucher):
        raise HTTPException(
            status_code=409,
            detail=f"validation の確認が必要です。確認内容を保存してから{operation}してください。",
        )


def _serialize_transfer_records(records: list[TransferRecordDraft]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for record in records:
        validation = record.validation_json or _validate_transfer_record(record)
        needs_review = bool(validation.get("needs_review", record.needs_review))
        serialized.append(
            {
                "id": _new_id("tr"),
                "vehicle_model": record.vehicle_model,
                "vehicle_number": record.vehicle_number,
                "pickup_datetime": record.pickup_datetime,
                "pickup_location": record.pickup_location,
                "delivery_datetime": record.delivery_datetime,
                "delivery_location": record.delivery_location,
                "confidence": record.confidence,
                "needs_review": needs_review,
                "review_status": "NEEDS_REVIEW" if needs_review else "AUTO_REVIEWED",
                "notes": record.notes,
                "validation_json": validation,
            }
        )
    return serialized


def _tables_for_transfer_llm(tables: list[ExtractedTable]) -> list[dict[str, Any]]:
    return [
        {
            "page": table.page,
            "table_index": table.table_index,
            "title": table.title,
            "headers": table.headers,
            "rows": table.rows,
        }
        for table in tables
    ]


def _clean_transfer_cell(value: Any) -> str:
    return str(value or "").replace("\u3000", " ").strip()


def _sanitize_transfer_location(value: Any) -> str | None:
    text = _clean_transfer_cell(value)
    if not text:
        return None

    text = re.sub(r"\b(?:TEL|FAX|PHONE)\s*[:：]?\s*\d[\d\s()（）+\-－ー]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "", text)
    text = re.sub(
        r"(?:ご?担当者?|担当|受付|窓口|連絡先|依頼者|ドライバー|運転者|CONTACT|ATTN)\s*[:：]?\s*[^,，/／;；\n\r]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^\s,，/／;；]+(?:様|さん|氏)", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,，/／;；])\s*", r"\1", text).strip(" ,，/／;；")
    if not text or _looks_like_person_only_location(text):
        return None
    return text


def _looks_like_person_only_location(value: Any) -> bool:
    text = _clean_transfer_cell(value)
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "〒",
            "株式会社",
            "有限会社",
            "合同会社",
            "工場",
            "営業所",
            "センター",
            "ヤード",
            "車体",
            "試験場",
            "作業",
            "港",
            "倉庫",
            "都",
            "道",
            "府",
            "県",
            "市",
            "区",
            "町",
            "村",
            "丁目",
            "番地",
            "号",
        )
    ):
        return False
    if re.search(r"(?:担当|受付|窓口|依頼者|ドライバー|運転者|様|さん|氏)", text):
        return True
    normalized = re.sub(r"[\s　]+", "", text)
    return bool(re.fullmatch(r"[一-龥ぁ-ん]{2,10}", normalized))


def _join_location_parts(parts: list[str]) -> str | None:
    cleaned = []
    seen = set()
    for part in parts:
        normalized = _sanitize_transfer_location(part)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return " ".join(cleaned) if cleaned else None


def _join_nonempty_cells(cells: list[Any]) -> str | None:
    return _join_location_parts([_clean_transfer_cell(cell) for cell in cells])


def _join_transfer_value_cells(cells: list[Any], skip_tokens: set[str], *, skip_vehicle_like: bool = False) -> str | None:
    values = []
    for cell in cells:
        value = _clean_transfer_cell(cell)
        if not value:
            continue
        if any(token in value for token in skip_tokens):
            continue
        if any(token in value for token in ("希望", "期", "車 型", "車型", "所在地", "所在場所", "搬入場所", "搬⼊場所")):
            continue
        if skip_vehicle_like and _looks_like_vehicle_value(value) and not re.search(r"\d{1,2}\s*(月|日|:)", value):
            continue
        values.append(value)
    return _join_location_parts(values)


def _looks_like_location_table(table: ExtractedTable) -> bool:
    if len(table.headers) < 3:
        return False
    table_text = " ".join(_clean_transfer_cell(cell) for row in [table.headers, *table.rows] for cell in row)
    if len(table.headers) > 5 and any(token in table_text for token in ("引取可能", "搬入希望", "搬⼊希望")):
        return False
    rows = table.rows or []
    has_tel = any(any("TEL" in _clean_transfer_cell(cell).upper() for cell in row) for row in rows)
    has_from_to = any(
        token in _clean_transfer_cell(header).upper()
        for header in table.headers
        for token in ("FROM", "TO")
    )
    compact_rows = " ".join(_clean_transfer_cell(cell) for row in rows for cell in row)
    return has_from_to or has_tel or ("住所" in compact_rows and ("名称" in compact_rows or "場所" in compact_rows))


def _extract_location_pair_from_tables(tables: list[ExtractedTable]) -> tuple[str | None, str | None]:
    for table in tables:
        if len(table.headers) >= 3 and "出発地" in _clean_transfer_cell(table.headers[0]):
            pickup = _sanitize_transfer_location(table.headers[2])
            delivery = None
            for row in table.rows:
                if len(row) >= 3 and "到着地" in _clean_transfer_cell(row[0]):
                    delivery = _sanitize_transfer_location(row[2])
                    break
            if pickup or delivery:
                return pickup or None, delivery or None
        if not _looks_like_location_table(table):
            continue
        pickup_parts: list[str] = []
        delivery_parts: list[str] = []
        for row in table.rows:
            if len(row) < 3:
                continue
            label = _clean_transfer_cell(row[0]).upper()
            if (
                "備考" in label
                or "NOTE" in label
                or any(token in label for token in ("担当", "担当者", "受付", "窓口", "連絡先", "依頼者", "TEL", "FAX", "CONTACT", "ATTN"))
            ):
                continue
            pickup_value = _sanitize_transfer_location(row[1])
            delivery_value = _sanitize_transfer_location(row[2])
            if pickup_value:
                pickup_parts.append(pickup_value)
            if delivery_value:
                delivery_parts.append(delivery_value)
        pickup = _join_location_parts(pickup_parts)
        delivery = _join_location_parts(delivery_parts)
        if pickup or delivery:
            return pickup, delivery
    return None, None


def _extract_transfer_dates_from_tables(tables: list[ExtractedTable]) -> tuple[str | None, str | None]:
    def looks_like_phone(value: str) -> bool:
        normalized = _clean_transfer_cell(value)
        return bool(re.fullmatch(r"(TEL[:：]?\s*)?\d{2,5}-\d{1,4}-\d{3,4}", normalized, flags=re.IGNORECASE))

    def looks_like_date(value: str) -> bool:
        normalized = _clean_transfer_cell(value)
        if not normalized:
            return False
        if normalized.upper().startswith("TEL") or looks_like_phone(normalized):
            return False
        if re.search(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", normalized):
            return True
        if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", normalized):
            return True
        if re.search(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", normalized):
            return True
        if re.search(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}\s*(AM|PM)?", normalized, flags=re.IGNORECASE):
            return True
        return False

    pickup_date = None
    delivery_date = None
    for table in tables:
        if len(table.headers) >= 3 and "出発地" in _clean_transfer_cell(table.headers[0]):
            for row in table.rows:
                if len(row) < 3:
                    continue
                label = _clean_transfer_cell(row[1])
                value = _clean_transfer_cell(row[2])
                if "搬出日時" in label and value:
                    pickup_date = value
                if "搬入日時" in label and value:
                    delivery_date = value
        for row in table.rows:
            if len(row) >= 2:
                label = _clean_transfer_cell(row[0])
                value = _clean_transfer_cell(row[1])
                label_is_schedule = any(token in label for token in ("輸送予定日", "予定日", "アレンジ日", "搬出日", "納期"))
                if label_is_schedule and looks_like_date(value):
                    pickup_date = pickup_date or value
                    delivery_date = delivery_date or value
                    continue
            if len(row) < 3:
                continue
            first_cell = _clean_transfer_cell(row[0])
            if "TEL" in first_cell.upper():
                continue
            pickup_candidate = _clean_transfer_cell(row[1])
            delivery_candidate = _clean_transfer_cell(row[2])
            if not looks_like_date(pickup_candidate) and not looks_like_date(delivery_candidate):
                continue
            if looks_like_date(pickup_candidate):
                pickup_date = pickup_candidate
            if looks_like_date(delivery_candidate):
                delivery_date = delivery_candidate
    return pickup_date, delivery_date


def _enrich_transfer_records_from_tables(records: list[dict[str, Any]], extraction: ExtractionResult) -> list[dict[str, Any]]:
    pickup_location, delivery_location = _extract_location_pair_from_tables(extraction.tables)
    pickup_datetime, delivery_datetime = _extract_transfer_dates_from_tables(extraction.tables)
    for record in records:
        existing_pickup_location = _sanitize_transfer_location(record.get("pickup_location"))
        existing_delivery_location = _sanitize_transfer_location(record.get("delivery_location"))
        record["pickup_location"] = existing_pickup_location
        record["delivery_location"] = existing_delivery_location
        if pickup_location and not existing_pickup_location:
            record["pickup_location"] = pickup_location
        if delivery_location and not existing_delivery_location:
            record["delivery_location"] = delivery_location
        if _clean_transfer_cell(record.get("pickup_datetime")).upper().startswith("TEL"):
            record["pickup_datetime"] = None
        if _clean_transfer_cell(record.get("delivery_datetime")).upper().startswith("TEL"):
            record["delivery_datetime"] = None
        if re.fullmatch(r"\d{2,5}-\d{1,4}-\d{3,4}", _clean_transfer_cell(record.get("pickup_datetime"))):
            record["pickup_datetime"] = None
        if re.fullmatch(r"\d{2,5}-\d{1,4}-\d{3,4}", _clean_transfer_cell(record.get("delivery_datetime"))):
            record["delivery_datetime"] = None
        if pickup_datetime and not record.get("pickup_datetime"):
            record["pickup_datetime"] = pickup_datetime
        if delivery_datetime and not record.get("delivery_datetime"):
            record["delivery_datetime"] = delivery_datetime
        draft = TransferRecordDraft(
            vehicle_model=record.get("vehicle_model"),
            vehicle_number=record.get("vehicle_number"),
            pickup_datetime=record.get("pickup_datetime"),
            pickup_location=record.get("pickup_location"),
            delivery_datetime=record.get("delivery_datetime"),
            delivery_location=record.get("delivery_location"),
            confidence=_coerce_transfer_confidence(record.get("confidence")),
            notes=record.get("notes"),
        )
        record["validation_json"] = _validate_transfer_record(draft)
        record["needs_review"] = bool(record["validation_json"].get("needs_review"))
        record["review_status"] = "NEEDS_REVIEW" if record["needs_review"] else record.get("review_status", "AUTO_REVIEWED")
    return records


def _coerce_transfer_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _looks_like_vehicle_value(value: Any) -> bool:
    text = _clean_transfer_cell(value)
    if not text:
        return False
    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    return bool(
        re.search(r"\b[A-Z]{2,}\d{2,}[A-Z0-9-]*\b", text)
        or re.search(r"\b[A-Z]{2}\d{2}[A-Z]{2}-\d+\b", text)
        or re.search(r"\b\d{6,}\b", text)
        or re.search(r"[A-Z]{2,}\d{2,}.*\d{4,}", text)
        or re.fullmatch(r"[A-Z]{2,}\d{2,}[A-Z0-9]*\d{4,}", compact)
    )


def _normalize_vehicle_number(value: Any) -> str | None:
    text = _clean_transfer_cell(value)
    if not text:
        return None
    text = re.sub(r"[\uff70\uff8c°º・･]", "-", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or None


def _normalize_transfer_datetime(value: Any) -> str | None:
    text = _clean_transfer_cell(value)
    if not text:
        return None
    text = text.lstrip("'")
    text = re.sub(r"\s+", " ", text)
    return text or None


def _append_record_if_vehicle(records: list[TransferRecordDraft], record: TransferRecordDraft) -> None:
    record.vehicle_number = _normalize_vehicle_number(record.vehicle_number)
    record.pickup_datetime = _normalize_transfer_datetime(record.pickup_datetime)
    record.delivery_datetime = _normalize_transfer_datetime(record.delivery_datetime)
    if not (_looks_like_vehicle_value(record.vehicle_model) or _looks_like_vehicle_value(record.vehicle_number)):
        return
    record.validation_json = _validate_transfer_record(record)
    records.append(record)


def _extract_vehicle_records_from_standard_table(table: ExtractedTable) -> list[TransferRecordDraft]:
    headers = [_clean_transfer_cell(header).lower() for header in table.headers]

    def find_col(*keywords: str) -> int | None:
        for index, header in enumerate(headers):
            if any(keyword.lower() in header for keyword in keywords):
                return index
        return None

    model_index = find_col("model", "車型", "車種", "型式", "車体番号")
    number_index = find_col("vin", "車番", "車台", "登録", "管理番号")
    if model_index is None and number_index is None:
        return []

    records: list[TransferRecordDraft] = []
    for row in table.rows:
        vehicle_model = _clean_transfer_cell(row[model_index]) if model_index is not None and model_index < len(row) else None
        vehicle_number = _clean_transfer_cell(row[number_index]) if number_index is not None and number_index < len(row) else None
        if vehicle_model and "(" in vehicle_model and ")" in vehicle_model and not vehicle_number:
            vehicle_number = vehicle_model.split("(", 1)[0].strip()
            vehicle_model = vehicle_model.split("(", 1)[1].rsplit(")", 1)[0].strip()
        record = TransferRecordDraft(
            vehicle_model=vehicle_model or None,
            vehicle_number=vehicle_number or None,
            confidence=0.65,
            needs_review=True,
            notes=f"table:{table.page}-{table.table_index}",
        )
        _append_record_if_vehicle(records, record)
    return records


def _extract_vehicle_records_from_request_grid(table: ExtractedTable) -> list[TransferRecordDraft]:
    records: list[TransferRecordDraft] = []
    rows = table.rows
    table_text = " ".join(_clean_transfer_cell(cell) for row in [table.headers, *rows] for cell in row)
    if not any(token in table_text for token in ("引取可能", "搬入希望", "搬⼊希望", "所在地", "搬入場所", "搬⼊場所")):
        return records
    for index, row in enumerate(rows):
        row_text = " ".join(_clean_transfer_cell(cell) for cell in row)
        if "車番" not in row_text and "車 番" not in row_text and "車台" not in row_text and "番" not in row_text:
            continue

        vehicle_number = None
        for cell in row:
            candidate = _clean_transfer_cell(cell)
            if _looks_like_vehicle_value(candidate):
                vehicle_number = candidate
                break
        if not vehicle_number:
            continue

        if index <= 3:
            header_row = table.headers
            time_row = rows[0] if len(rows) > 0 else []
            location_row = rows[1] if len(rows) > 1 else []
            model_row = table.headers
        else:
            header_row = rows[index - 3]
            time_row = rows[index - 2]
            location_row = rows[index - 1]
            model_row = rows[index - 3]
        pickup_datetime = _join_transfer_value_cells(
            [*header_row[2:8], *time_row[2:7]],
            {"引取可能", "期日", "車 型", "車型"},
            skip_vehicle_like=True,
        )
        delivery_datetime = _join_transfer_value_cells(
            [*header_row[7:13], *time_row[7:12]],
            {"搬入希望", "搬⼊希望", "期日"},
            skip_vehicle_like=True,
        )
        vehicle_model = None
        for cell in [*model_row, *table.headers]:
            candidate = _clean_transfer_cell(cell)
            if re.search(r"\b[A-Z]{2,}\d{2,}[A-Z0-9-]*\b", candidate):
                vehicle_model = candidate
                break

        pickup_location = _join_transfer_value_cells(location_row[2:7], {"所在地", "引取場所"})
        delivery_location = _join_transfer_value_cells([*location_row[7:12], *location_row[15:18]], {"搬入場所", "搬⼊場所"})

        record = TransferRecordDraft(
            vehicle_model=vehicle_model,
            vehicle_number=vehicle_number,
            pickup_datetime=pickup_datetime,
            pickup_location=pickup_location,
            delivery_datetime=delivery_datetime,
            delivery_location=delivery_location,
            confidence=0.6,
            needs_review=True,
            notes=f"grid:{table.page}-{table.table_index}",
        )
        _append_record_if_vehicle(records, record)
    return records


def _build_transfer_records_from_llm(source_filename: str, extraction: ExtractionResult) -> tuple[list[dict[str, Any]], list[str]]:
    result, error, attempted = extract_transfer_rows_with_ollama(
        source_filename,
        extraction.raw_text,
        _tables_for_transfer_llm(extraction.tables),
    )
    if not result:
        return [], [error] if attempted and error else []

    records: list[TransferRecordDraft] = []
    for row in result.get("rows") or []:
        if not isinstance(row, dict):
            continue
        vehicle_model = row.get("vehicle_model")
        vehicle_number = row.get("vehicle_number")
        vehicle_label = row.get("vehicle_label")
        if vehicle_label and (not vehicle_model or not vehicle_number):
            label_parts = [part.strip() for part in str(vehicle_label).split("/") if part.strip()]
            if len(label_parts) >= 2:
                vehicle_model = vehicle_model or label_parts[0]
                vehicle_number = vehicle_number or label_parts[-1]
        record = TransferRecordDraft(
            vehicle_model=vehicle_model,
            vehicle_number=vehicle_number,
            pickup_datetime=row.get("pickup_datetime"),
            pickup_location=row.get("pickup_location"),
            delivery_datetime=row.get("delivery_datetime"),
            delivery_location=row.get("delivery_location"),
            confidence=_coerce_transfer_confidence(row.get("confidence")),
            notes=row.get("notes"),
        )
        record.validation_json = _validate_transfer_record(record)
        records.append(record)
    return _enrich_transfer_records_from_tables(_serialize_transfer_records(records), extraction), list(result.get("warnings") or [])


def _build_transfer_records_from_tables(source_filename: str, extraction: ExtractionResult) -> list[dict[str, Any]]:
    records: list[TransferRecordDraft] = []
    for table in extraction.tables:
        records.extend(_extract_vehicle_records_from_standard_table(table))
        records.extend(_extract_vehicle_records_from_request_grid(table))
    return _enrich_transfer_records_from_tables(_serialize_transfer_records(records[:50]), extraction)


def _unique_path(destination_dir: Path, filename: str) -> Path:
    candidate = destination_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        next_candidate = destination_dir / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    return destination_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _accept_inbox_file(voucher_type: str, source_path: Path, batch_id: str | None = None) -> str:
    voucher_id = _new_id("v")
    stored_path = settings.upload_dir / f"{voucher_id}{source_path.suffix.lower()}"
    shutil.copy2(source_path, stored_path)
    insert_voucher(
        _build_voucher_payload(
            voucher_id=voucher_id,
            batch_id=batch_id,
            voucher_type=voucher_type,
            source_filename=source_path.name,
            source_path=str(stored_path),
            status="OCR_PROCESSING",
        )
    )
    append_audit_log(_new_id("log"), voucher_id, "INBOX_ACCEPTED", {"filename": source_path.name, "voucher_type": voucher_type, "batch_id": batch_id})
    processed_path = _unique_path(settings.processed_dir, source_path.name)
    shutil.move(str(source_path), processed_path)
    return voucher_id


def _build_review_fields(voucher_type: str, form: Any) -> dict[str, ExtractedField]:
    specs = FIELD_SPECS.get(voucher_type, FIELD_SPECS["invoice"])
    reviewed_values = {
        "issue_date": form.get("issue_date") or None,
        "due_date": form.get("due_date") or None,
        "document_number": form.get("document_number") or None,
        "vendor_name": form.get("vendor_name") or None,
        "customer_name": form.get("customer_name") or None,
        "currency": form.get("currency") or "JPY",
        "subtotal": _to_float(form.get("subtotal")),
        "tax": _to_float(form.get("tax")),
        "discount": _to_float(form.get("discount")),
        "grand_total": _to_float(form.get("grand_total")),
        "notes": form.get("notes") or None,
    }

    fields: dict[str, ExtractedField] = {}
    for key, value in reviewed_values.items():
        required = bool(specs.get(key, {}).get("required"))
        missing = value in (None, "")
        fields[key] = ExtractedField(
            key=key,
            value=value,
            raw_text=None if missing else str(value),
            confidence=0.0 if missing else 1.0,
            bbox=None,
            needs_review=required and missing,
            source="review",
        )
    return fields


def _build_review_document_json(
    voucher: dict[str, Any],
    voucher_type: str,
    fields: dict[str, ExtractedField],
    items: list[VoucherItemDraft],
    validation: dict[str, Any],
) -> dict[str, Any]:
    document_json = dict(voucher.get("document_json") or {})
    document_json["voucher_type"] = voucher_type
    document_json["fields"] = {key: asdict(field) for key, field in fields.items()}
    document_json["items"] = [asdict(item) for item in items]
    document_json["warnings"] = validation["warnings"]
    document_json.setdefault("raw_text", "")
    document_json.setdefault("ocr_lines", [])
    document_json.setdefault("tables", [])
    document_json.setdefault("context_hints", [])
    document_json.setdefault("llm_used", False)
    document_json.setdefault("llm_status", "unused")
    document_json.setdefault("llm_messages", [])
    return document_json


def _build_voucher_payload(
    voucher_id: str,
    batch_id: str | None,
    voucher_type: str,
    source_filename: str,
    source_path: str,
    status: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "id": voucher_id,
        "batch_id": batch_id,
        "type": voucher_type,
        "status": status,
        "needs_review": 0,
        "source_filename": source_filename,
        "source_path": source_path,
        "issue_date": None,
        "due_date": None,
        "document_number": None,
        "vendor_name": None,
        "customer_name": None,
        "currency": "JPY",
        "subtotal": None,
        "tax": None,
        "discount": None,
        "grand_total": None,
        "confidence": 0.0,
        "notes": None,
        "document_json": "{}",
        "raw_ocr_json": "{}",
        "validation_json": "{}",
        "exported_at": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _accept_upload(voucher_type: str, upload: UploadFile, batch_id: str | None = None) -> str:
    voucher_id = _new_id("v")
    suffix = Path(upload.filename or "upload.bin").suffix
    stored_path = settings.upload_dir / f"{voucher_id}{suffix}"

    with stored_path.open("wb") as destination:
        shutil.copyfileobj(upload.file, destination)

    insert_voucher(
        _build_voucher_payload(
            voucher_id=voucher_id,
            batch_id=batch_id,
            voucher_type=voucher_type,
            source_filename=upload.filename or stored_path.name,
            source_path=str(stored_path),
            status="OCR_PROCESSING",
        )
    )
    append_audit_log(_new_id("log"), voucher_id, "UPLOAD_ACCEPTED", {"filename": upload.filename, "voucher_type": voucher_type, "batch_id": batch_id})
    return voucher_id


def _source_preview_kind(source_path: str) -> str | None:
    suffix = Path(source_path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return "image"
    return None


def _resolve_voucher_source_path(voucher: dict[str, Any]) -> Path:
    source_path = Path(voucher["source_path"]).resolve()
    upload_root = settings.upload_dir.resolve()
    try:
        source_path.relative_to(upload_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid source path") from exc
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source file not found")
    return source_path


def process_voucher_ocr(voucher_id: str) -> None:
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        _log_ocr(f"skip missing voucher voucher_id={voucher_id}")
        return

    try:
        source_path = Path(voucher["source_path"])
        _log_ocr(f"start voucher_id={voucher_id} file={source_path.name} type={voucher['type']}")
        _log_ocr(f"run OCR voucher_id={voucher_id}")
        lines = run_ocr(source_path)
        _log_ocr(f"OCR done voucher_id={voucher_id} lines={len(lines)}")
        _log_ocr(f"extract tables voucher_id={voucher_id}")
        tables = extract_tables(source_path, ocr_lines=lines)
        _log_ocr(f"tables done voucher_id={voucher_id} tables={len(tables)}")
        _log_ocr(f"extract fields voucher_id={voucher_id}")
        extraction = extract_document(voucher["type"], lines, tables=tables)
        _log_ocr(f"fields done voucher_id={voucher_id} items={len(extraction.items)}")
        validation = validate_extraction(extraction)
        transfer_records, transfer_warnings = _build_transfer_records_from_llm(voucher["source_filename"], extraction)
        if not transfer_records:
            transfer_records = _build_transfer_records_from_tables(voucher["source_filename"], extraction)
        validation["warnings"] = [*validation.get("warnings", []), *transfer_warnings]
        if transfer_warnings:
            validation["issues_detected"] = True
        if transfer_records:
            replace_transfer_records(voucher_id, transfer_records)
            _log_ocr(f"transfer records done voucher_id={voucher_id} records={len(transfer_records)}")
        elif voucher["type"] == "delivery":
            validation["warnings"] = [*validation.get("warnings", []), "陸送情報が抽出されませんでした。元伝票を確認してください。"]
            validation["issues_detected"] = True
        validation = _finalize_validation(validation, transfer_records)
        _log_ocr(f"validation done voucher_id={voucher_id} status={validation['status']} score={validation['confidence_score']}")
        field_values = {key: field.value for key, field in extraction.fields.items()}
        max_confidence = max((field.confidence for field in extraction.fields.values()), default=0.0)
        items = [
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit": item.unit,
                "unit_price": item.unit_price,
                "amount": item.amount,
                "tax_rate": item.tax_rate,
                "confidence": item.confidence,
                "needs_review": item.needs_review,
            }
            for item in extraction.items
        ]

        payload = {
            "type": voucher["type"],
            "status": validation["status"],
            "needs_review": int(validation["needs_review"]),
            "issue_date": field_values.get("issue_date"),
            "due_date": field_values.get("due_date"),
            "document_number": field_values.get("document_number"),
            "vendor_name": field_values.get("vendor_name"),
            "customer_name": field_values.get("customer_name"),
            "currency": field_values.get("currency") or "JPY",
            "subtotal": field_values.get("subtotal"),
            "tax": field_values.get("tax"),
            "discount": field_values.get("discount"),
            "grand_total": field_values.get("grand_total"),
            "confidence": round(max_confidence, 3),
            "notes": field_values.get("notes"),
            "document_json": json.dumps(extraction.to_dict(), ensure_ascii=False),
            "raw_ocr_json": json.dumps(
                {
                    "ocr_lines": [asdict(line) for line in extraction.ocr_lines],
                    "tables": [asdict(table) for table in extraction.tables],
                },
                ensure_ascii=False,
            ),
            "validation_json": json.dumps(validation, ensure_ascii=False),
            "exported_at": voucher.get("exported_at"),
            "updated_at": now_iso(),
        }
        update_voucher(voucher_id, payload, _serialize_items_for_db(voucher_id, items))
        append_audit_log(_new_id("log"), voucher_id, "OCR_COMPLETED", {"status": validation["status"], "warnings": validation["warnings"]})
        _log_ocr(f"completed voucher_id={voucher_id} status={validation['status']} warnings={len(validation['warnings'])}")
    except Exception as exc:  # noqa: BLE001
        _log_ocr(f"failed voucher_id={voucher_id} error={exc}")
        payload = {
            "type": voucher["type"],
            "status": "OCR_FAILED",
            "needs_review": 1,
            "issue_date": voucher.get("issue_date"),
            "due_date": voucher.get("due_date"),
            "document_number": voucher.get("document_number"),
            "vendor_name": voucher.get("vendor_name"),
            "customer_name": voucher.get("customer_name"),
            "currency": voucher.get("currency") or "JPY",
            "subtotal": voucher.get("subtotal"),
            "tax": voucher.get("tax"),
            "discount": voucher.get("discount"),
            "grand_total": voucher.get("grand_total"),
            "confidence": 0.0,
            "notes": voucher.get("notes"),
            "document_json": json.dumps(voucher.get("document_json", {}), ensure_ascii=False),
            "raw_ocr_json": json.dumps(voucher.get("raw_ocr_json", {}), ensure_ascii=False),
            "validation_json": json.dumps({"status": "OCR_FAILED", "needs_review": True, "warnings": [str(exc)]}, ensure_ascii=False),
            "exported_at": voucher.get("exported_at"),
            "updated_at": now_iso(),
        }
        update_voucher(voucher_id, payload, _serialize_items_for_db(voucher_id, voucher.get("items", [])))
        append_audit_log(_new_id("log"), voucher_id, "OCR_FAILED", {"error": str(exc)})


def _on_ocr_done(future: Future[None]) -> None:
    OCR_FUTURES.discard(future)
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        # process_voucher_ocr handles per-voucher failures; this is a last-resort guard.
        print(f"OCR worker failed unexpectedly: {exc}")


def enqueue_voucher_ocr(voucher_id: str) -> None:
    _log_ocr(f"queued voucher_id={voucher_id} active={len(OCR_FUTURES)}")
    future = OCR_EXECUTOR.submit(process_voucher_ocr, voucher_id)
    OCR_FUTURES.add(future)
    future.add_done_callback(_on_ocr_done)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    if not settings.requeue_processing_ocr_on_startup:
        return
    for voucher in fetch_all_vouchers():
        if voucher.get("status") == "OCR_PROCESSING":
            append_audit_log(_new_id("log"), voucher["id"], "OCR_REQUEUED", {"reason": "startup"})
            enqueue_voucher_ocr(voucher["id"])


@app.on_event("shutdown")
def on_shutdown() -> None:
    OCR_EXECUTOR.shutdown(wait=False, cancel_futures=False)


@app.get("/")
def index(request: Request):
    vouchers = fetch_all_vouchers()
    current_batch_id = request.query_params.get("batch_id") or ""
    current_batch_vouchers = [voucher for voucher in vouchers if voucher.get("batch_id") == current_batch_id] if current_batch_id else []
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "settings": settings,
            "vouchers": vouchers,
            "voucher_type_labels": VOUCHER_TYPE_LABELS,
            "status_labels": STATUS_LABELS,
            "current_batch_id": current_batch_id,
            "current_batch_count": len(current_batch_vouchers),
        },
    )


@app.post("/upload")
async def upload_voucher(
    voucher_type: str = Form(...),
    files: list[UploadFile] = File(...),
):
    created_ids: list[str] = []
    batch_id = _new_id("batch")
    for upload in files:
        if not (upload.filename or "").strip():
            continue
        voucher_id = _accept_upload(voucher_type, upload, batch_id=batch_id)
        created_ids.append(voucher_id)
        enqueue_voucher_ocr(voucher_id)

    if not created_ids:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(created_ids) == 1:
        return RedirectResponse(url=f"/vouchers/{created_ids[0]}", status_code=303)
    return RedirectResponse(url=f"/?batch_id={batch_id}", status_code=303)


@app.post("/import/inbox")
def import_inbox(voucher_type: str = Form("delivery")):
    created_ids: list[str] = []
    batch_id = _new_id("batch")
    for source_path in sorted(settings.inbox_dir.iterdir()):
        if not source_path.is_file() or source_path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            continue
        voucher_id = _accept_inbox_file(voucher_type, source_path, batch_id=batch_id)
        created_ids.append(voucher_id)
        enqueue_voucher_ocr(voucher_id)

    if created_ids:
        return RedirectResponse(url=f"/vouchers/{created_ids[0]}" if len(created_ids) == 1 else f"/?batch_id={batch_id}", status_code=303)
    raise HTTPException(status_code=400, detail="No supported files found in inbox")


@app.get("/demo/empty-review")
def empty_review_demo(request: Request):
    return templates.TemplateResponse(
        request,
        "voucher_detail.html",
        {
            "request": request,
            "settings": settings,
            "voucher": _empty_review_demo_voucher(),
            "voucher_type_labels": VOUCHER_TYPE_LABELS,
            "status_labels": STATUS_LABELS,
            "field_labels": FIELD_LABELS,
            "source_preview_kind": None,
            "source_preview_url": "",
            "demo_empty_result": True,
        },
    )


@app.get("/vouchers/{voucher_id}")
def voucher_detail(request: Request, voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    source_preview_kind = _source_preview_kind(voucher["source_path"])
    return templates.TemplateResponse(
        request,
        "voucher_detail.html",
        {
            "request": request,
            "settings": settings,
            "voucher": voucher,
            "voucher_type_labels": VOUCHER_TYPE_LABELS,
            "status_labels": STATUS_LABELS,
            "field_labels": FIELD_LABELS,
            "source_preview_kind": source_preview_kind,
            "source_preview_url": f"/vouchers/{voucher_id}/source",
            "demo_empty_result": False,
        },
    )


@app.get("/vouchers/{voucher_id}/source")
def voucher_source(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    source_path = _resolve_voucher_source_path(voucher)
    media_type = guess_type(str(source_path))[0] or "application/octet-stream"
    return FileResponse(
        source_path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{voucher["source_filename"]}"'},
    )


@app.post("/vouchers/{voucher_id}/review")
async def review_voucher(request: Request, voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")

    form = await request.form()
    voucher_type = form.get("type") or voucher["type"]
    item_ids = form.getlist("item_id")
    descriptions = form.getlist("item_description")
    quantities = form.getlist("item_quantity")
    units = form.getlist("item_unit")
    unit_prices = form.getlist("item_unit_price")
    amounts = form.getlist("item_amount")
    tax_rates = form.getlist("item_tax_rate")
    transfer_ids = form.getlist("transfer_id")
    transfer_vehicle_models = form.getlist("transfer_vehicle_model")
    transfer_vehicle_numbers = form.getlist("transfer_vehicle_number")
    transfer_pickup_datetimes = form.getlist("transfer_pickup_datetime")
    transfer_pickup_locations = form.getlist("transfer_pickup_location")
    transfer_delivery_datetimes = form.getlist("transfer_delivery_datetime")
    transfer_delivery_locations = form.getlist("transfer_delivery_location")
    transfer_notes = form.getlist("transfer_notes")

    reviewed_items: list[VoucherItemDraft] = []
    for index, description in enumerate(descriptions):
        if not any(
            [
                description,
                quantities[index] if index < len(quantities) else "",
                unit_prices[index] if index < len(unit_prices) else "",
                amounts[index] if index < len(amounts) else "",
            ]
        ):
            continue
        reviewed_items.append(
            VoucherItemDraft(
                description=description,
                quantity=_to_float(quantities[index]) if index < len(quantities) else None,
                unit=units[index] if index < len(units) else None,
                unit_price=_to_float(unit_prices[index]) if index < len(unit_prices) else None,
                amount=_to_float(amounts[index]) if index < len(amounts) else None,
                tax_rate=_to_float(tax_rates[index]) if index < len(tax_rates) else None,
                confidence=1.0,
                needs_review=False,
            )
        )

    fields = _build_review_fields(voucher_type, form)
    required_warnings = [
        f"必須項目 {key} を入力してください。"
        for key, field in fields.items()
        if field.needs_review
    ]
    document_json = voucher.get("document_json", {})
    extraction = ExtractionResult(
        voucher_type=voucher_type,
        fields=fields,
        items=reviewed_items,
        warnings=required_warnings,
        raw_text=document_json.get("raw_text", ""),
        tables=[],
        llm_used=bool(document_json.get("llm_used")),
    )
    serialized_items = [
        {
            "id": item_ids[index] if index < len(item_ids) and item_ids[index] else None,
            "description": item.description,
            "quantity": item.quantity,
            "unit": item.unit,
            "unit_price": item.unit_price,
            "amount": item.amount,
            "tax_rate": item.tax_rate,
            "confidence": item.confidence,
            "needs_review": item.needs_review,
        }
        for index, item in enumerate(reviewed_items)
    ]

    reviewed_transfer_records: list[dict[str, Any]] = []
    for index, vehicle_model in enumerate(transfer_vehicle_models):
        vehicle_number = transfer_vehicle_numbers[index] if index < len(transfer_vehicle_numbers) else ""
        pickup_location = transfer_pickup_locations[index] if index < len(transfer_pickup_locations) else ""
        delivery_location = transfer_delivery_locations[index] if index < len(transfer_delivery_locations) else ""
        if not any([vehicle_model, vehicle_number, pickup_location, delivery_location]):
            continue
        record = TransferRecordDraft(
            vehicle_model=vehicle_model or None,
            vehicle_number=vehicle_number or None,
            pickup_datetime=transfer_pickup_datetimes[index] if index < len(transfer_pickup_datetimes) and transfer_pickup_datetimes[index] else None,
            pickup_location=pickup_location or None,
            delivery_datetime=transfer_delivery_datetimes[index] if index < len(transfer_delivery_datetimes) and transfer_delivery_datetimes[index] else None,
            delivery_location=delivery_location or None,
            confidence=1.0,
            notes=transfer_notes[index] if index < len(transfer_notes) and transfer_notes[index] else None,
        )
        validation_json = _validate_transfer_record(record)
        reviewed_transfer_records.append(
            {
                "id": transfer_ids[index] if index < len(transfer_ids) and transfer_ids[index] else _new_id("tr"),
                "vehicle_model": record.vehicle_model,
                "vehicle_number": record.vehicle_number,
                "pickup_datetime": record.pickup_datetime,
                "pickup_location": record.pickup_location,
                "delivery_datetime": record.delivery_datetime,
                "delivery_location": record.delivery_location,
                "confidence": record.confidence,
                "needs_review": validation_json["needs_review"],
                "notes": record.notes,
                "validation_json": validation_json,
            }
        )
    confirmation_checked = str(form.get("manual_confirmation_completed") or "").lower() in {"1", "true", "yes", "on"}
    validation = validate_extraction(extraction)
    if voucher_type == "delivery" and not reviewed_transfer_records:
        validation["issues_detected"] = True
        validation["warnings"] = [*validation.get("warnings", []), "陸送情報が入力されていません。元伝票を確認してください。"]
    if _requires_uncompleted_confirmation(voucher) and not confirmation_checked:
        validation["issues_detected"] = True
        validation["warnings"] = [
            *validation.get("warnings", []),
            "前回の validation 判定について、元伝票との照合確認を完了してください。",
        ]
    validation = _finalize_validation(
        validation,
        reviewed_transfer_records,
        manual_confirmation_completed=confirmation_checked,
    )
    if validation["manual_confirmation_completed"]:
        for record in reviewed_transfer_records:
            if record.get("needs_review"):
                record["needs_review"] = False
                record["validation_json"] = {
                    **(record.get("validation_json") or {}),
                    "manual_confirmation_completed": True,
                }
    payload = {
        "type": voucher_type,
        "status": validation["status"],
        "needs_review": int(validation["needs_review"]),
        "issue_date": fields["issue_date"].value,
        "due_date": fields["due_date"].value,
        "document_number": fields["document_number"].value,
        "vendor_name": fields["vendor_name"].value,
        "customer_name": fields["customer_name"].value,
        "currency": fields["currency"].value or "JPY",
        "subtotal": fields["subtotal"].value,
        "tax": fields["tax"].value,
        "discount": fields["discount"].value,
        "grand_total": fields["grand_total"].value,
        "confidence": float(form.get("confidence") or voucher.get("confidence") or 0.0),
        "notes": fields["notes"].value,
        "document_json": json.dumps(
            _build_review_document_json(voucher, voucher_type, fields, reviewed_items, validation),
            ensure_ascii=False,
        ),
        "raw_ocr_json": json.dumps(voucher.get("raw_ocr_json", {}), ensure_ascii=False),
        "validation_json": json.dumps(validation, ensure_ascii=False),
        "exported_at": voucher.get("exported_at"),
        "updated_at": now_iso(),
    }

    update_voucher(voucher_id, payload, _serialize_items_for_db(voucher_id, serialized_items))
    if reviewed_transfer_records or voucher.get("transfer_records"):
        update_transfer_records(voucher_id, reviewed_transfer_records, reviewer="local")
    append_audit_log(
        _new_id("log"),
        voucher_id,
        "REVIEW_SAVED",
        {
            "status": payload["status"],
            "validation_score": validation.get("confidence_score"),
            "manual_confirmation_completed": validation.get("manual_confirmation_completed"),
        },
    )
    return RedirectResponse(url=f"/vouchers/{voucher_id}", status_code=303)


@app.post("/vouchers/{voucher_id}/transition")
async def transition_voucher(voucher_id: str, action: str = Form(...)):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")

    mapping = {
        "submit": "READY_FOR_APPROVAL",
        "approve": "APPROVED",
        "reject": "REJECTED",
        "reopen": "REVIEW_REQUIRED",
    }
    if action not in mapping:
        raise HTTPException(status_code=400, detail="Unknown transition")
    if action in {"submit", "approve"}:
        _require_completed_confirmation(voucher, "状態変更")

    update_status(voucher_id, mapping[action])
    append_audit_log(_new_id("log"), voucher_id, "STATUS_CHANGED", {"action": action, "status": mapping[action]})
    return RedirectResponse(url=f"/vouchers/{voucher_id}", status_code=303)


@app.get("/vouchers/{voucher_id}/export/xlsx")
def export_xlsx(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    _require_completed_confirmation(voucher, "Excel 出力")
    export_path = export_voucher_xlsx(voucher)
    update_status(voucher_id, "EXPORTED", exported_at=now_iso())
    append_audit_log(_new_id("log"), voucher_id, "EXPORTED_XLSX", {"path": str(export_path)})
    return FileResponse(export_path, filename=export_path.name)


@app.get("/vouchers/{voucher_id}/export/csv")
def export_csv(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    _require_completed_confirmation(voucher, "CSV 出力")
    export_path = export_voucher_csv_zip(voucher)
    update_status(voucher_id, "EXPORTED", exported_at=now_iso())
    append_audit_log(_new_id("log"), voucher_id, "EXPORTED_CSV", {"path": str(export_path)})
    return FileResponse(export_path, filename=export_path.name)


@app.get("/export/transfer-summary.xlsx")
def export_transfer_summary(operator_name: str | None = None, batch_id: str | None = None):
    voucher_rows = fetch_all_vouchers()
    if batch_id:
        voucher_rows = [voucher for voucher in voucher_rows if voucher.get("batch_id") == batch_id]
    vouchers = [fetch_voucher(voucher["id"]) for voucher in voucher_rows]
    blocked_vouchers = [
        voucher
        for voucher in vouchers
        if voucher is not None and voucher.get("transfer_records") and _requires_uncompleted_confirmation(voucher)
    ]
    if blocked_vouchers:
        raise HTTPException(
            status_code=409,
            detail="validation の確認が完了していない伝票が含まれています。各伝票を確認してから Excel 出力してください。",
        )
    export_path = export_transfer_summary_xlsx([voucher for voucher in vouchers if voucher is not None], operator_name)
    append_audit_log(_new_id("log"), None, "EXPORTED_TRANSFER_SUMMARY_XLSX", {"path": str(export_path), "batch_id": batch_id})
    return FileResponse(export_path, filename=export_path.name)


@app.get("/api/v1/vouchers")
def api_list_vouchers():
    return {"data": fetch_all_vouchers()}


@app.get("/api/v1/vouchers/{voucher_id}")
def api_get_voucher(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    return {"data": voucher}
