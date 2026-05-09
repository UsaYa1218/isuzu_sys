from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import settings


logger = logging.getLogger(__name__)


def _base_headers() -> dict[str, str]:
    headers = {str(key): str(value) for key, value in (settings.ollama_headers or {}).items()}
    if settings.ollama_api_key.strip() and "authorization" not in {key.lower() for key in headers}:
        headers["Authorization"] = f"Bearer {settings.ollama_api_key.strip()}"
    return headers


def _detect_api_style() -> str:
    style = settings.ollama_api_style
    if style in {"ollama", "openai", "openai-responses", "openai-chat"}:
        return style

    normalized_base = settings.ollama_base_url.rstrip("/").lower()
    if normalized_base.endswith("/v1"):
        return "openai"
    if settings.ollama_model.startswith("openai/"):
        return "openai"
    return "ollama"


def _ollama_think_value() -> bool | str | None:
    raw_value = settings.ollama_think.strip()
    if not raw_value:
        return None

    normalized = raw_value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return raw_value


def _openai_responses_url() -> str:
    base_url = settings.ollama_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/responses"
    return f"{base_url}/v1/responses"


def _openai_chat_completions_url() -> str:
    base_url = settings.ollama_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _openai_chat_response_format(response_schema: dict[str, Any] | None) -> dict[str, Any]:
    if response_schema is None:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "voucher_extraction",
            "schema": response_schema,
            "strict": True,
        },
    }


def _openai_responses_text_format(response_schema: dict[str, Any] | None) -> dict[str, Any]:
    if response_schema is None:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "name": "voucher_extraction",
        "schema": response_schema,
        "strict": True,
    }


def _extract_openai_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

    return "\n".join(parts).strip()


def _extract_openai_chat_output_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""

    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"].strip())
        return "\n".join(part for part in parts if part).strip()
    return ""


def _strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_json_fragment(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text.strip()

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1].strip()

    return text[start:].strip()


def _parse_json_body(body: str, *, required_top_level_keys: set[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    normalized = _strip_markdown_code_fence(body)
    candidates = [normalized]
    fragment = _extract_json_fragment(normalized)
    if fragment and fragment not in candidates:
        candidates.append(fragment)

    last_error = "LLM response was not valid JSON."
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"LLM response was not valid JSON: {exc}"
            continue

        if not isinstance(parsed, dict):
            return None, "LLM response JSON root was not an object."
        if not parsed:
            return None, "LLM returned an empty JSON object."
        if required_top_level_keys and not required_top_level_keys.intersection(parsed):
            return None, "LLM JSON did not contain the expected top-level keys."
        return parsed, None

    return None, last_error


def _normalization_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["fields", "items", "context_hints", "warnings"],
        "properties": {
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "issue_date",
                    "due_date",
                    "document_number",
                    "vendor_name",
                    "customer_name",
                    "currency",
                    "subtotal",
                    "tax",
                    "discount",
                    "grand_total",
                    "notes",
                ],
                "properties": {
                    "issue_date": {"type": ["string", "null"]},
                    "due_date": {"type": ["string", "null"]},
                    "document_number": {"type": ["string", "null"]},
                    "vendor_name": {"type": ["string", "null"]},
                    "customer_name": {"type": ["string", "null"]},
                    "currency": {"type": ["string", "null"]},
                    "subtotal": {"type": ["number", "null"]},
                    "tax": {"type": ["number", "null"]},
                    "discount": {"type": ["number", "null"]},
                    "grand_total": {"type": ["number", "null"]},
                    "notes": {"type": ["string", "null"]},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["description", "quantity", "unit", "unit_price", "amount", "tax_rate"],
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "amount": {"type": ["number", "null"]},
                        "tax_rate": {"type": ["number", "null"]},
                    },
                },
            },
            "context_hints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "value", "field_key", "reason", "confidence"],
                    "properties": {
                        "kind": {"type": "string"},
                        "value": {"type": "string"},
                        "field_key": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                        "confidence": {"type": ["number", "null"]},
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _table_reconstruction_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tables", "items", "warnings"],
        "properties": {
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "headers", "rows"],
                    "properties": {
                        "title": {"type": "string"},
                        "headers": {"type": "array", "items": {"type": "string"}},
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["description", "quantity", "unit", "unit_price", "amount", "tax_rate"],
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "amount": {"type": ["number", "null"]},
                        "tax_rate": {"type": ["number", "null"]},
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _generate_json_via_ollama(
    prompt: str,
    *,
    response_schema: dict[str, Any] | None = None,
    required_top_level_keys: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    options = {"temperature": 0, **settings.ollama_generate_options}
    request_payload: dict[str, Any] = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": response_schema or "json",
        "options": options,
    }
    think_value = _ollama_think_value()
    if think_value is not None:
        request_payload["think"] = think_value

    try:
        response = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            headers=_base_headers() or None,
            json=request_payload,
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        body = payload.get("response", "").strip()
        if not body:
            return None, "LLM returned an empty response.", True
        parsed, parse_error = _parse_json_body(body, required_top_level_keys=required_top_level_keys)
        if parsed is not None:
            return parsed, None, True
        logger.warning("Ollama JSON parse failed: %s", parse_error)
        return None, parse_error, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama request skipped: %s", exc)
        return None, f"LLM request failed: {type(exc).__name__}: {exc}", True


def _generate_json_via_openai_responses(
    prompt: str,
    *,
    response_schema: dict[str, Any] | None = None,
    required_top_level_keys: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    request_body: dict[str, Any] = {
        "model": settings.ollama_model,
        "input": prompt,
        "temperature": 0,
        "text": {"format": _openai_responses_text_format(response_schema)},
    }
    options = settings.ollama_generate_options or {}
    if "reasoning_effort" in options:
        request_body["reasoning"] = {"effort": str(options["reasoning_effort"])}

    try:
        response = httpx.post(
            _openai_responses_url(),
            headers=_base_headers() or None,
            json=request_body,
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        body = _extract_openai_output_text(payload)
        if not body:
            return None, "LLM returned an empty response.", True
        parsed, parse_error = _parse_json_body(body, required_top_level_keys=required_top_level_keys)
        if parsed is not None:
            return parsed, None, True
        logger.warning("OpenAI-compatible JSON parse failed: %s", parse_error)
        return None, parse_error, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenAI-compatible request skipped: %s", exc)
        return None, f"LLM request failed: {type(exc).__name__}: {exc}", True


def _generate_json_via_openai_chat_completions(
    prompt: str,
    *,
    response_schema: dict[str, Any] | None = None,
    required_top_level_keys: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    def build_request_body(schema: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "model": settings.ollama_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return valid JSON only. Do not wrap the JSON in Markdown.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": _openai_chat_response_format(schema),
        }

    request_body = build_request_body(response_schema)

    try:
        response = httpx.post(
            _openai_chat_completions_url(),
            headers=_base_headers() or None,
            json=request_body,
            timeout=settings.ollama_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        body = _extract_openai_chat_output_text(payload)
        if not body:
            return None, "LLM returned an empty response.", True
        parsed, parse_error = _parse_json_body(body, required_top_level_keys=required_top_level_keys)
        if parsed is not None:
            return parsed, None, True
        logger.warning("OpenAI chat-completions JSON parse failed: %s", parse_error)
        return None, parse_error, True
    except Exception as exc:  # noqa: BLE001
        if response_schema is not None:
            logger.info("OpenAI chat-completions schema request failed; retrying with json_object: %s", exc)
            return _generate_json_via_openai_chat_completions(
                prompt,
                response_schema=None,
                required_top_level_keys=required_top_level_keys,
            )
        logger.warning("OpenAI chat-completions request skipped: %s", exc)
        return None, f"LLM request failed: {type(exc).__name__}: {exc}", True


def _generate_json_via_openai_compatible(
    prompt: str,
    *,
    response_schema: dict[str, Any] | None = None,
    required_top_level_keys: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    style = _detect_api_style()
    if style == "openai-chat":
        return _generate_json_via_openai_chat_completions(
            prompt,
            response_schema=response_schema,
            required_top_level_keys=required_top_level_keys,
        )

    result = _generate_json_via_openai_responses(
        prompt,
        response_schema=response_schema,
        required_top_level_keys=required_top_level_keys,
    )
    if style == "openai-responses" or result[0] is not None:
        return result

    logger.info("OpenAI Responses API did not produce usable JSON; falling back to chat completions: %s", result[1])
    return _generate_json_via_openai_chat_completions(
        prompt,
        response_schema=response_schema,
        required_top_level_keys=required_top_level_keys,
    )


def _generate_json(
    prompt: str,
    *,
    response_schema: dict[str, Any] | None = None,
    required_top_level_keys: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    if not settings.ollama_base_url.strip() or not settings.ollama_model.strip():
        return None, "LLM is not configured.", False

    if _detect_api_style() in {"openai", "openai-responses", "openai-chat"}:
        return _generate_json_via_openai_compatible(
            prompt,
            response_schema=response_schema,
            required_top_level_keys=required_top_level_keys,
        )
    return _generate_json_via_ollama(
        prompt,
        response_schema=response_schema,
        required_top_level_keys=required_top_level_keys,
    )


def _normalization_prompt(voucher_type: str, raw_text: str, heuristic_draft: dict[str, Any]) -> str:
    return f"""
You normalize OCR output into structured voucher data.
Use both table text and non-table text. Preserve useful names, addresses, phone numbers, and memo-like text.

Rules:
- Do not invent facts that are not grounded in OCR text.
- Use surrounding narrative text too, not only table cells.
- If a company name or person name strongly looks like the issuer or sender, map it to `vendor_name`.
- If a company name or person name strongly looks like the receiver or customer, map it to `customer_name`.
- Keep useful snippets that do not cleanly fit the schema in `context_hints`.
- Unknown values must stay null.
- Dates must be `YYYY-MM-DD`.
- Numbers must be JSON numbers.
- Return JSON only.

JSON shape:
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
  "context_hints": [
    {{
      "kind": "person_name",
      "value": "",
      "field_key": null,
      "reason": "",
      "confidence": 0.0
    }}
  ],
  "warnings": []
}}

Allowed `context_hints.kind` values:
`person_name`, `company_name`, `address`, `phone`, `email`, `memo`, `document_context`.

Set `field_key` when a hint likely maps to one of:
`issue_date`, `due_date`, `document_number`, `vendor_name`, `customer_name`, `currency`, `subtotal`, `tax`, `discount`, `grand_total`, `notes`.

Voucher type: {voucher_type}

Heuristic draft:
{json.dumps(heuristic_draft, ensure_ascii=False, indent=2)}

OCR text:
{raw_text}
""".strip()


def _table_reconstruction_prompt(voucher_type: str, raw_text: str, heuristic_tables: list[dict[str, Any]]) -> str:
    return f"""
You reconstruct low-quality OCR tables into cleaner table structures.

Rules:
- Do not invent facts that are not grounded in OCR text.
- If a cell is unknown, keep it empty.
- Keep headers in the original reading order.
- Return JSON only.

JSON shape:
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

Voucher type: {voucher_type}

Detected tables:
{json.dumps(heuristic_tables, ensure_ascii=False, indent=2)}

OCR text:
{raw_text}
""".strip()


def normalize_with_ollama(
    voucher_type: str,
    raw_text: str,
    heuristic_draft: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    return _generate_json(
        _normalization_prompt(voucher_type, raw_text, heuristic_draft),
        response_schema=_normalization_response_schema(),
        required_top_level_keys={"fields", "items", "context_hints", "warnings"},
    )


def reconstruct_tables_with_ollama(
    voucher_type: str,
    raw_text: str,
    heuristic_tables: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    if not heuristic_tables:
        return None, None, False
    return _generate_json(
        _table_reconstruction_prompt(voucher_type, raw_text, heuristic_tables),
        response_schema=_table_reconstruction_response_schema(),
        required_top_level_keys={"tables", "items", "warnings"},
    )
