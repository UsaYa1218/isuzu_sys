from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import settings


logger = logging.getLogger(__name__)


def _generate_json(prompt: str) -> tuple[dict[str, Any] | None, str | None, bool]:
    if not settings.ollama_base_url.strip() or not settings.ollama_model.strip():
        return None, "LLM設定が未入力です。", False

    options = {"temperature": 0, **settings.ollama_generate_options}
    headers = settings.ollama_headers or None
    try:
        response = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            headers=headers,
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": options,
            },
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        body = payload.get("response", "").strip()
        if not body:
            return None, "LLMから空のレスポンスが返されました。", True
        try:
            return json.loads(body), None, True
        except json.JSONDecodeError as exc:
            logger.warning("Ollama JSON decode failed: %s", exc)
            return None, "LLMのJSON解析に失敗しました。", True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama request skipped: %s", exc)
        return None, f"LLM実行失敗: {type(exc).__name__}: {exc}", True


def _normalization_prompt(voucher_type: str, raw_text: str, heuristic_draft: dict[str, Any]) -> str:
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


def _table_reconstruction_prompt(voucher_type: str, raw_text: str, heuristic_tables: list[dict[str, Any]]) -> str:
    return f"""
あなたは OCR 後の帳票再構成モデルです。
壊れた表や結合された表を、人がレビューしやすい表へ整理してください。

制約:
- OCR に無い情報を推測して補わない
- 異なる表が混ざっている場合は分割する
- ヘッダは短い日本語にする
- 値が曖昧なセルは空文字にする
- 必ず JSON のみ返す

JSON 形式:
{{
  "tables": [
    {{
      "title": "",
      "headers": [""],
      "rows": [[""]]
    }}
  ],
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

暫定表:
{json.dumps(heuristic_tables, ensure_ascii=False, indent=2)}

OCR テキスト:
{raw_text}
""".strip()


def normalize_with_ollama(
    voucher_type: str,
    raw_text: str,
    heuristic_draft: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    return _generate_json(_normalization_prompt(voucher_type, raw_text, heuristic_draft))


def reconstruct_tables_with_ollama(
    voucher_type: str,
    raw_text: str,
    heuristic_tables: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    if not heuristic_tables:
        return None, None, False
    return _generate_json(_table_reconstruction_prompt(voucher_type, raw_text, heuristic_tables))
