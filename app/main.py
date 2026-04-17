from __future__ import annotations

import json
from mimetypes import guess_type
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
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
    update_status,
    update_voucher,
)
from .services.exporter import export_voucher_csv_zip, export_voucher_xlsx
from .services.extraction import FIELD_SPECS, extract_document
from .services.ocr_pipeline import extract_tables, run_ocr
from .services.validation import validate_extraction
from .schemas import ExtractedField, ExtractionResult, VoucherItemDraft


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(settings.templates_path))
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
init_db()

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


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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
    document_json.setdefault("llm_used", False)
    document_json.setdefault("llm_status", "unused")
    document_json.setdefault("llm_messages", [])
    return document_json


def _build_voucher_payload(
    voucher_id: str,
    voucher_type: str,
    source_filename: str,
    source_path: str,
    status: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "id": voucher_id,
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


def _accept_upload(voucher_type: str, upload: UploadFile) -> str:
    voucher_id = _new_id("v")
    suffix = Path(upload.filename or "upload.bin").suffix
    stored_path = settings.upload_dir / f"{voucher_id}{suffix}"

    with stored_path.open("wb") as destination:
        shutil.copyfileobj(upload.file, destination)

    insert_voucher(
        _build_voucher_payload(
            voucher_id=voucher_id,
            voucher_type=voucher_type,
            source_filename=upload.filename or stored_path.name,
            source_path=str(stored_path),
            status="OCR_PROCESSING",
        )
    )
    append_audit_log(_new_id("log"), voucher_id, "UPLOAD_ACCEPTED", {"filename": upload.filename, "voucher_type": voucher_type})
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
        return

    try:
        source_path = Path(voucher["source_path"])
        lines = run_ocr(source_path)
        tables = extract_tables(source_path, ocr_lines=lines)
        extraction = extract_document(voucher["type"], lines, tables=tables)
        validation = validate_extraction(extraction)
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
    except Exception as exc:  # noqa: BLE001
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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index(request: Request):
    vouchers = fetch_all_vouchers()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "settings": settings,
            "vouchers": vouchers,
            "voucher_type_labels": VOUCHER_TYPE_LABELS,
            "status_labels": STATUS_LABELS,
        },
    )


@app.post("/upload")
async def upload_voucher(
    background_tasks: BackgroundTasks,
    voucher_type: str = Form(...),
    files: list[UploadFile] = File(...),
):
    created_ids: list[str] = []
    for upload in files:
        if not (upload.filename or "").strip():
            continue
        voucher_id = _accept_upload(voucher_type, upload)
        created_ids.append(voucher_id)
        background_tasks.add_task(process_voucher_ocr, voucher_id)

    if not created_ids:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(created_ids) == 1:
        return RedirectResponse(url=f"/vouchers/{created_ids[0]}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


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
    validation = validate_extraction(extraction)
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
    append_audit_log(_new_id("log"), voucher_id, "REVIEW_SAVED", {"status": payload["status"]})
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

    update_status(voucher_id, mapping[action])
    append_audit_log(_new_id("log"), voucher_id, "STATUS_CHANGED", {"action": action, "status": mapping[action]})
    return RedirectResponse(url=f"/vouchers/{voucher_id}", status_code=303)


@app.get("/vouchers/{voucher_id}/export/xlsx")
def export_xlsx(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    export_path = export_voucher_xlsx(voucher)
    update_status(voucher_id, "EXPORTED", exported_at=now_iso())
    append_audit_log(_new_id("log"), voucher_id, "EXPORTED_XLSX", {"path": str(export_path)})
    return FileResponse(export_path, filename=export_path.name)


@app.get("/vouchers/{voucher_id}/export/csv")
def export_csv(voucher_id: str):
    voucher = fetch_voucher(voucher_id)
    if voucher is None:
        raise HTTPException(status_code=404, detail="Voucher not found")
    export_path = export_voucher_csv_zip(voucher)
    update_status(voucher_id, "EXPORTED", exported_at=now_iso())
    append_audit_log(_new_id("log"), voucher_id, "EXPORTED_CSV", {"path": str(export_path)})
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
