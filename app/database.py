from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import settings


def _dict_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def get_connection() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=30)
    connection.row_factory = _dict_factory
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def connection_scope() -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def init_db() -> None:
    with connection_scope() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS vouchers (
                id TEXT PRIMARY KEY,
                batch_id TEXT,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                source_filename TEXT NOT NULL,
                source_path TEXT NOT NULL,
                issue_date TEXT,
                due_date TEXT,
                document_number TEXT,
                vendor_name TEXT,
                customer_name TEXT,
                currency TEXT DEFAULT 'JPY',
                subtotal REAL,
                tax REAL,
                discount REAL,
                grand_total REAL,
                confidence REAL DEFAULT 0,
                notes TEXT,
                document_json TEXT NOT NULL DEFAULT '{}',
                raw_ocr_json TEXT NOT NULL DEFAULT '{}',
                validation_json TEXT NOT NULL DEFAULT '{}',
                exported_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS voucher_items (
                id TEXT PRIMARY KEY,
                voucher_id TEXT NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
                line_no INTEGER NOT NULL,
                description TEXT,
                quantity REAL,
                unit TEXT,
                unit_price REAL,
                amount REAL,
                tax_rate REAL,
                confidence REAL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                voucher_id TEXT,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transfer_records (
                id TEXT PRIMARY KEY,
                voucher_id TEXT NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
                line_no INTEGER NOT NULL,
                vehicle_model TEXT,
                vehicle_number TEXT,
                pickup_datetime TEXT,
                pickup_location TEXT,
                delivery_datetime TEXT,
                delivery_location TEXT,
                confidence REAL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 1,
                review_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
                notes TEXT,
                validation_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT
            );
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(vouchers)").fetchall()}
        if "batch_id" not in columns:
            connection.execute("ALTER TABLE vouchers ADD COLUMN batch_id TEXT")


def fetch_all_vouchers() -> list[dict[str, Any]]:
    with connection_scope() as connection:
        cursor = connection.execute(
            """
            SELECT
                id,
                batch_id,
                type,
                status,
                needs_review,
                source_filename,
                issue_date,
                document_number,
                vendor_name,
                customer_name,
                grand_total,
                currency,
                confidence,
                created_at,
                updated_at
            FROM vouchers
            ORDER BY created_at DESC
            """
        )
        vouchers = cursor.fetchall()
        counts = connection.execute(
            """
            SELECT
                voucher_id,
                COUNT(*) AS transfer_record_count,
                SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS transfer_review_count
            FROM transfer_records
            GROUP BY voucher_id
            """
        ).fetchall()
        count_map = {row["voucher_id"]: row for row in counts}
        for voucher in vouchers:
            row = count_map.get(voucher["id"], {})
            voucher["transfer_record_count"] = row.get("transfer_record_count", 0)
            voucher["transfer_review_count"] = row.get("transfer_review_count", 0) or 0
        return vouchers


def fetch_voucher(voucher_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        voucher = connection.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        if voucher is None:
            return None
        items = connection.execute(
            """
            SELECT *
            FROM voucher_items
            WHERE voucher_id = ?
            ORDER BY line_no ASC
            """,
            (voucher_id,),
        ).fetchall()
        logs = connection.execute(
            """
            SELECT *
            FROM audit_logs
            WHERE voucher_id = ?
            ORDER BY created_at DESC
            """,
            (voucher_id,),
        ).fetchall()
        transfer_records = connection.execute(
            """
            SELECT *
            FROM transfer_records
            WHERE voucher_id = ?
            ORDER BY line_no ASC
            """,
            (voucher_id,),
        ).fetchall()
    voucher["document_json"] = json.loads(voucher.get("document_json") or "{}")
    voucher["raw_ocr_json"] = json.loads(voucher.get("raw_ocr_json") or "{}")
    voucher["validation_json"] = json.loads(voucher.get("validation_json") or "{}")
    voucher["document_json"].setdefault("tables", [])
    voucher["document_json"].setdefault("ocr_lines", [])
    voucher["document_json"].setdefault("warnings", [])
    voucher["document_json"].setdefault("llm_used", False)
    voucher["document_json"].setdefault("llm_status", "unused")
    voucher["document_json"].setdefault("llm_messages", [])
    voucher["document_json"].setdefault("context_hints", [])
    voucher["items"] = items
    for record in transfer_records:
        record["validation_json"] = json.loads(record.get("validation_json") or "{}")
    voucher["transfer_records"] = transfer_records
    voucher["audit_logs"] = logs
    return voucher


def insert_voucher(payload: dict[str, Any]) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO vouchers (
                id,
                batch_id,
                type,
                status,
                needs_review,
                source_filename,
                source_path,
                issue_date,
                due_date,
                document_number,
                vendor_name,
                customer_name,
                currency,
                subtotal,
                tax,
                discount,
                grand_total,
                confidence,
                notes,
                document_json,
                raw_ocr_json,
                validation_json,
                exported_at,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :batch_id,
                :type,
                :status,
                :needs_review,
                :source_filename,
                :source_path,
                :issue_date,
                :due_date,
                :document_number,
                :vendor_name,
                :customer_name,
                :currency,
                :subtotal,
                :tax,
                :discount,
                :grand_total,
                :confidence,
                :notes,
                :document_json,
                :raw_ocr_json,
                :validation_json,
                :exported_at,
                :created_at,
                :updated_at
            )
            """,
            payload,
        )


def replace_voucher_items(connection: sqlite3.Connection, voucher_id: str, items: list[dict[str, Any]]) -> None:
    connection.execute("DELETE FROM voucher_items WHERE voucher_id = ?", (voucher_id,))
    for item in items:
        connection.execute(
            """
            INSERT INTO voucher_items (
                id,
                voucher_id,
                line_no,
                description,
                quantity,
                unit,
                unit_price,
                amount,
                tax_rate,
                confidence,
                needs_review
            ) VALUES (
                :id,
                :voucher_id,
                :line_no,
                :description,
                :quantity,
                :unit,
                :unit_price,
                :amount,
                :tax_rate,
                :confidence,
                :needs_review
            )
            """,
            item,
        )


def update_voucher(voucher_id: str, payload: dict[str, Any], items: list[dict[str, Any]]) -> None:
    with connection_scope() as connection:
        payload["voucher_id"] = voucher_id
        connection.execute(
            """
            UPDATE vouchers
            SET
                type = :type,
                status = :status,
                needs_review = :needs_review,
                issue_date = :issue_date,
                due_date = :due_date,
                document_number = :document_number,
                vendor_name = :vendor_name,
                customer_name = :customer_name,
                currency = :currency,
                subtotal = :subtotal,
                tax = :tax,
                discount = :discount,
                grand_total = :grand_total,
                confidence = :confidence,
                notes = :notes,
                document_json = :document_json,
                raw_ocr_json = :raw_ocr_json,
                validation_json = :validation_json,
                exported_at = :exported_at,
                updated_at = :updated_at
            WHERE id = :voucher_id
            """,
            payload,
        )
        replace_voucher_items(connection, voucher_id, items)


def replace_transfer_records(voucher_id: str, records: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    with connection_scope() as connection:
        connection.execute("DELETE FROM transfer_records WHERE voucher_id = ?", (voucher_id,))
        for line_no, record in enumerate(records, start=1):
            connection.execute(
                """
                INSERT INTO transfer_records (
                    id,
                    voucher_id,
                    line_no,
                    vehicle_model,
                    vehicle_number,
                    pickup_datetime,
                    pickup_location,
                    delivery_datetime,
                    delivery_location,
                    confidence,
                    needs_review,
                    review_status,
                    notes,
                    validation_json,
                    created_at,
                    updated_at,
                    reviewed_by,
                    reviewed_at
                ) VALUES (
                    :id,
                    :voucher_id,
                    :line_no,
                    :vehicle_model,
                    :vehicle_number,
                    :pickup_datetime,
                    :pickup_location,
                    :delivery_datetime,
                    :delivery_location,
                    :confidence,
                    :needs_review,
                    :review_status,
                    :notes,
                    :validation_json,
                    :created_at,
                    :updated_at,
                    :reviewed_by,
                    :reviewed_at
                )
                """,
                {
                    "id": record.get("id"),
                    "voucher_id": voucher_id,
                    "line_no": line_no,
                    "vehicle_model": record.get("vehicle_model"),
                    "vehicle_number": record.get("vehicle_number"),
                    "pickup_datetime": record.get("pickup_datetime"),
                    "pickup_location": record.get("pickup_location"),
                    "delivery_datetime": record.get("delivery_datetime"),
                    "delivery_location": record.get("delivery_location"),
                    "confidence": record.get("confidence", 0.0),
                    "needs_review": int(bool(record.get("needs_review", True))),
                    "review_status": record.get("review_status") or "NEEDS_REVIEW",
                    "notes": record.get("notes"),
                    "validation_json": json.dumps(record.get("validation_json") or {}, ensure_ascii=False),
                    "created_at": record.get("created_at") or timestamp,
                    "updated_at": timestamp,
                    "reviewed_by": record.get("reviewed_by"),
                    "reviewed_at": record.get("reviewed_at"),
                },
            )


def update_transfer_records(voucher_id: str, records: list[dict[str, Any]], reviewer: str | None = None) -> None:
    timestamp = now_iso()
    prepared: list[dict[str, Any]] = []
    for record in records:
        needs_review = bool(record.get("needs_review"))
        prepared.append(
            {
                **record,
                "review_status": "NEEDS_REVIEW" if needs_review else "REVIEWED",
                "reviewed_by": reviewer,
                "reviewed_at": None if needs_review else timestamp,
                "updated_at": timestamp,
            }
        )
    replace_transfer_records(voucher_id, prepared)


def update_status(voucher_id: str, status: str, exported_at: str | None = None) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE vouchers
            SET status = ?, exported_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, exported_at, now_iso(), voucher_id),
        )


def append_audit_log(log_id: str, voucher_id: str | None, action: str, detail: dict[str, Any]) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO audit_logs (id, voucher_id, action, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (log_id, voucher_id, action, json.dumps(detail, ensure_ascii=False), now_iso()),
        )
