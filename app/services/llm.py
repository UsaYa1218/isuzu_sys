from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import settings


logger = logging.getLogger(__name__)


def _prompt(voucher_type: str, raw_text: str, heuristic_draft: dict[str, Any]) -> str:
    return f"""
あなたは伝票転記補助モデルです。次の OCR テキストとヒューリスティック抽出結果を正規化してください。

制約:
- OCR に無い情報を推測して埋めない
- 不明な値は null のまま返す
- 数値は数値型、日付は YYYY-MM-DD、税率は 0.10 のような小数で返す
- 必ず JSON のみ返す

JSON 形式:
{{
  "fields": {{
    "issue_date": null,
    "due_date": null,
    "document_number": null,
    "vendor_name": null,
    "customer_name": null,
    "currency": "JPY",
    "subtotal": null,
    "tax": null,
    "discount": null,
    "grand_total": null,
    "notes": null
  }},
  "items": [
    {{
      "description": "",
      "quantity": null,
      "unit": null,
      "unit_price": null,
      "amount": null,
      "tax_rate": null
    }}
  ],
  "warnings": []
}}

伝票種別: {voucher_type}

ヒューリスティック抽出:
{json.dumps(heuristic_draft, ensure_ascii=False, indent=2)}

OCR テキスト:
{raw_text}
""".strip()


def normalize_with_ollama(voucher_type: str, raw_text: str, heuristic_draft: dict[str, Any]) -> dict[str, Any] | None:
    prompt = _prompt(voucher_type, raw_text, heuristic_draft)
    try:
        response = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        body = payload.get("response", "").strip()
        if not body:
            return None
        return json.loads(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama normalization skipped: %s", exc)
        return None
