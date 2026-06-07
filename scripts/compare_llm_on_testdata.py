from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
settings: Any | None = None
FIELD_SPECS: dict[str, dict[str, dict[str, Any]]] = {}
ExtractedTable = None
OCRLine = None
extract_document = None
extract_tables = None
run_ocr = None
validate_extraction = None
ocr_pipeline_module = None


def _ensure_app_modules_loaded() -> None:
    global settings
    global FIELD_SPECS
    global ExtractedTable
    global OCRLine
    global extract_document
    global extract_tables
    global run_ocr
    global validate_extraction
    global ocr_pipeline_module

    if settings is not None:
        return

    import app.services.ocr_pipeline as loaded_ocr_pipeline_module
    from app.config import settings as loaded_settings
    from app.schemas import ExtractedTable as loaded_extracted_table
    from app.schemas import OCRLine as loaded_ocr_line
    from app.services.extraction import FIELD_SPECS as loaded_field_specs
    from app.services.extraction import extract_document as loaded_extract_document
    from app.services.ocr_pipeline import extract_tables as loaded_extract_tables
    from app.services.ocr_pipeline import run_ocr as loaded_run_ocr
    from app.services.validation import validate_extraction as loaded_validate_extraction

    settings = loaded_settings
    FIELD_SPECS = loaded_field_specs
    ExtractedTable = loaded_extracted_table
    OCRLine = loaded_ocr_line
    extract_document = loaded_extract_document
    extract_tables = loaded_extract_tables
    run_ocr = loaded_run_ocr
    validate_extraction = loaded_validate_extraction
    ocr_pipeline_module = loaded_ocr_pipeline_module


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ocr_cache_path(cache_dir: Path, source_path: Path) -> Path:
    return cache_dir / f"{source_path.stem}.json"


def _load_ocr_cache(cache_dir: Path, source_path: Path) -> tuple[list[Any], list[Any]] | None:
    _ensure_app_modules_loaded()
    cache_path = _ocr_cache_path(cache_dir, source_path)
    if not cache_path.exists():
        return None

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    raw_lines = payload.get("ocr_lines") or []
    raw_tables = payload.get("tables") or []
    return [OCRLine(**line) for line in raw_lines], [ExtractedTable(**table) for table in raw_tables]


def _write_ocr_cache(cache_dir: Path, source_path: Path, ocr_lines: list[Any], tables: list[Any]) -> None:
    payload = {
        "source_file": source_path.name,
        "ocr_lines": [asdict(line) for line in ocr_lines],
        "tables": [asdict(table) for table in tables],
    }
    _json_dump(_ocr_cache_path(cache_dir, source_path), payload)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").split()).strip().lower()


def _normalize_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field_value_for_result(result_dict: dict[str, Any], field_key: str) -> Any:
    return (result_dict.get("fields") or {}).get(field_key, {}).get("value")


def _summarize_result(result_dict: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    fields = result_dict.get("fields") or {}
    field_count = len(fields)
    filled_fields = sum(1 for field in fields.values() if field.get("value") not in (None, ""))
    review_fields = sum(1 for field in fields.values() if field.get("needs_review"))
    items = result_dict.get("items") or []
    review_items = sum(1 for item in items if item.get("needs_review"))

    return {
        "voucher_type": result_dict.get("voucher_type"),
        "field_count": field_count,
        "filled_fields": filled_fields,
        "fill_rate": round(filled_fields / field_count, 3) if field_count else 0.0,
        "review_fields": review_fields,
        "items_count": len(items),
        "review_items": review_items,
        "context_hints_count": len(result_dict.get("context_hints") or []),
        "warnings_count": len(validation.get("warnings") or []),
        "llm_used": bool(result_dict.get("llm_used")),
        "llm_status": result_dict.get("llm_status") or "",
        "needs_review": bool(validation.get("needs_review")),
        "status": validation.get("status") or "",
    }


def _score_fields(
    expected_fields: dict[str, Any],
    result_dict: dict[str, Any],
    voucher_type: str,
) -> dict[str, Any]:
    specs = FIELD_SPECS.get(voucher_type, FIELD_SPECS["invoice"])
    details: list[dict[str, Any]] = []
    matched = 0

    for field_key, expected_value in expected_fields.items():
        predicted_value = _field_value_for_result(result_dict, field_key)
        field_type = specs.get(field_key, {}).get("type", "text")
        is_match = False

        if field_type in {"money", "number"}:
            expected_number = _normalize_number(expected_value)
            predicted_number = _normalize_number(predicted_value)
            if expected_number is not None and predicted_number is not None:
                is_match = abs(expected_number - predicted_number) <= 0.01
        else:
            is_match = _normalize_text(expected_value) == _normalize_text(predicted_value)

        if is_match:
            matched += 1

        details.append(
            {
                "field_key": field_key,
                "expected": expected_value,
                "predicted": predicted_value,
                "matched": is_match,
            }
        )

    total = len(details)
    return {
        "matched": matched,
        "total": total,
        "accuracy": round(matched / total, 3) if total else None,
        "details": details,
    }


def _sanitize_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value.strip())
    return safe.strip("_") or "default"


def _discover_cases(input_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        cases.append({"file": path.name, "voucher_type": "invoice"})
    return cases


def _load_cases(input_dir: Path, case_file: Path | None) -> list[dict[str, Any]]:
    if case_file and case_file.exists():
        payload = json.loads(case_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_cases = payload.get("cases") or []
        else:
            raw_cases = payload
        if not isinstance(raw_cases, list):
            raise ValueError("Case file must contain a list or an object with a 'cases' list.")
        return [dict(case) for case in raw_cases]
    return _discover_cases(input_dir)


def _write_case_template(input_dir: Path, destination: Path) -> None:
    cases = []
    for case in _discover_cases(input_dir):
        cases.append(
            {
                "file": case["file"],
                "voucher_type": "invoice",
                "expected_fields": {},
            }
        )
    _json_dump(destination, {"cases": cases})


def _load_profiles_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    _ensure_app_modules_loaded()
    profiles: list[dict[str, Any]] = []

    if args.ollama_model:
        for model_name in args.ollama_model:
            profiles.append(
                {
                    "name": _sanitize_name(model_name),
                    "base_url": args.base_url or settings.ollama_base_url,
                    "model": model_name,
                    "api_style": args.api_style or "ollama",
                    "api_key": args.api_key if args.api_key is not None else settings.ollama_api_key,
                }
            )
        return profiles

    if args.profile_file and args.profile_file.exists():
        payload = json.loads(args.profile_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_profiles = payload.get("profiles") or []
        else:
            raw_profiles = payload
        if not isinstance(raw_profiles, list):
            raise ValueError("Profile file must contain a list or an object with a 'profiles' list.")
        for raw in raw_profiles:
            profile = dict(raw)
            profile["name"] = profile.get("name") or _sanitize_name(str(profile.get("model") or "profile"))
            profile["base_url"] = profile.get("base_url") or settings.ollama_base_url
            profile["model"] = profile.get("model") or settings.ollama_model
            profile["api_style"] = (profile.get("api_style") or settings.ollama_api_style or "auto").strip().lower()
            profile["api_key"] = profile.get("api_key", settings.ollama_api_key)
            profiles.append(profile)
        return profiles

    return [
        {
            "name": _sanitize_name(settings.ollama_model),
            "base_url": settings.ollama_base_url,
            "model": settings.ollama_model,
            "api_style": settings.ollama_api_style,
            "api_key": settings.ollama_api_key,
        }
    ]


@contextmanager
def _override_llm_profile(profile: dict[str, Any]):
    original = {
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": settings.ollama_model,
        "ollama_api_style": settings.ollama_api_style,
        "ollama_api_key": settings.ollama_api_key,
        "ollama_headers": dict(settings.ollama_headers or {}),
        "ollama_generate_options": dict(settings.ollama_generate_options or {}),
    }
    try:
        settings.ollama_base_url = str(profile["base_url"])
        settings.ollama_model = str(profile["model"])
        settings.ollama_api_style = str(profile.get("api_style") or "auto")
        settings.ollama_api_key = str(profile.get("api_key") or "")
        settings.ollama_headers = dict(profile.get("headers") or original["ollama_headers"])
        settings.ollama_generate_options = dict(profile.get("options") or original["ollama_generate_options"])
        yield
    finally:
        settings.ollama_base_url = original["ollama_base_url"]
        settings.ollama_model = original["ollama_model"]
        settings.ollama_api_style = original["ollama_api_style"]
        settings.ollama_api_key = original["ollama_api_key"]
        settings.ollama_headers = original["ollama_headers"]
        settings.ollama_generate_options = original["ollama_generate_options"]


@contextmanager
def _override_ocr_settings(*, disable_remote_ocr: bool, local_ocr_cpu: bool):
    _ensure_app_modules_loaded()
    original_remote_ocr_base_url = settings.remote_ocr_base_url
    original_paddleocr_use_gpu = settings.paddleocr_use_gpu

    try:
        if disable_remote_ocr:
            settings.remote_ocr_base_url = ""
        if local_ocr_cpu:
            settings.paddleocr_use_gpu = False

        if ocr_pipeline_module is not None:
            for cache_name in ("_get_gpu_ocr_engine", "_get_cpu_ocr_engine"):
                cache_target = getattr(ocr_pipeline_module, cache_name, None)
                if cache_target is not None and hasattr(cache_target, "cache_clear"):
                    cache_target.cache_clear()
        yield
    finally:
        settings.remote_ocr_base_url = original_remote_ocr_base_url
        settings.paddleocr_use_gpu = original_paddleocr_use_gpu
        if ocr_pipeline_module is not None:
            for cache_name in ("_get_gpu_ocr_engine", "_get_cpu_ocr_engine"):
                cache_target = getattr(ocr_pipeline_module, cache_name, None)
                if cache_target is not None and hasattr(cache_target, "cache_clear"):
                    cache_target.cache_clear()


def _build_output_dir(base_output_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = base_output_dir / f"llm_compare_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _write_summary_files(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    _json_dump(output_dir / "summary.json", {"rows": rows})

    fieldnames = [
        "file",
        "voucher_type",
        "profile",
        "status",
        "needs_review",
        "llm_status",
        "fill_rate",
        "filled_fields",
        "field_count",
        "review_fields",
        "items_count",
        "review_items",
        "context_hints_count",
        "warnings_count",
        "scored_fields",
        "matched_fields",
        "field_accuracy",
        "error",
    ]
    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})

    markdown_lines = [
        "# LLM Comparison Summary",
        "",
        "| file | voucher_type | profile | status | fill_rate | field_accuracy | matched/scored | warnings | error |",
        "| --- | --- | --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    for row in rows:
        scored = row.get("scored_fields")
        matched = row.get("matched_fields")
        matched_display = "-" if not scored else f"{matched}/{scored}"
        accuracy = "-" if row.get("field_accuracy") is None else f"{row['field_accuracy']:.3f}"
        markdown_lines.append(
            f"| {row['file']} | {row['voucher_type']} | {row['profile']} | {row['status']} | {row['fill_rate']:.3f} | {accuracy} | {matched_display} | {row['warnings_count']} | {row.get('error', '')} |"
        )
    (output_dir / "summary.md").write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare LLM extraction quality on testdata files.")
    parser.add_argument("--input-dir", type=Path, default=ROOT_DIR / "runtime" / "testdata")
    parser.add_argument("--case-file", type=Path, default=None)
    parser.add_argument("--only-file", action="append", default=[])
    parser.add_argument("--profile-file", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=ROOT_DIR / "runtime" / "llm_compare")
    parser.add_argument("--ollama-model", action="append", default=[])
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-style", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--write-case-template", type=Path, default=None)
    parser.add_argument("--disable-remote-ocr", action="store_true")
    parser.add_argument("--local-ocr-cpu", action="store_true")
    parser.add_argument("--reuse-ocr-cache", action="store_true")
    parser.add_argument("--refresh-ocr-cache", action="store_true")
    parser.add_argument("--ocr-only", action="store_true")
    parser.add_argument("--ocr-cache-dir", type=Path, default=ROOT_DIR / "runtime" / "ocr_cache")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory was not found: {input_dir}")

    if args.write_case_template:
        _write_case_template(input_dir, args.write_case_template.resolve())
        print(f"Case template written to: {args.write_case_template.resolve()}")
        return 0

    case_file = args.case_file.resolve() if args.case_file else None
    profile_file = args.profile_file.resolve() if args.profile_file else None
    _ensure_app_modules_loaded()
    cases = _load_cases(input_dir, case_file)
    profiles = _load_profiles_from_args(args)

    if args.only_file:
        only_files = {str(name) for name in args.only_file}
        cases = [case for case in cases if str(case.get("file")) in only_files]

    if args.limit > 0:
        cases = cases[: args.limit]

    if not cases:
        print("No test cases were found.")
        return 0

    if not profiles:
        raise ValueError("No LLM profiles were configured.")

    output_dir = _build_output_dir(args.output_dir.resolve())
    ocr_cache_dir = args.ocr_cache_dir.resolve()
    rows: list[dict[str, Any]] = []

    for case in cases:
        relative_file = str(case["file"])
        source_path = (input_dir / relative_file).resolve()
        if not source_path.exists():
            print(f"Skipping missing file: {relative_file}")
            continue

        voucher_type = str(case.get("voucher_type") or "invoice")
        expected_fields = dict(case.get("expected_fields") or {})

        print(f"[OCR] {relative_file}")
        try:
            cached_ocr = None
            if args.reuse_ocr_cache and not args.refresh_ocr_cache:
                cached_ocr = _load_ocr_cache(ocr_cache_dir, source_path)

            if cached_ocr is not None:
                ocr_lines, tables = cached_ocr
                print(f"  [OCR cache] {relative_file}")
            else:
                with _override_ocr_settings(
                    disable_remote_ocr=args.disable_remote_ocr,
                    local_ocr_cpu=args.local_ocr_cpu,
                ):
                    ocr_lines = run_ocr(source_path)
                    tables = extract_tables(source_path, ocr_lines=ocr_lines)
                if args.reuse_ocr_cache or args.refresh_ocr_cache:
                    _write_ocr_cache(ocr_cache_dir, source_path, ocr_lines, tables)
        except Exception as exc:  # noqa: BLE001
            for profile in profiles:
                rows.append(
                    {
                        "file": relative_file,
                        "voucher_type": voucher_type,
                        "profile": profile["name"],
                        "status": "OCR_FAILED",
                        "needs_review": True,
                        "llm_status": "skipped",
                        "fill_rate": 0.0,
                        "filled_fields": 0,
                        "field_count": 0,
                        "review_fields": 0,
                        "items_count": 0,
                        "review_items": 0,
                        "context_hints_count": 0,
                        "warnings_count": 1,
                        "scored_fields": 0,
                        "matched_fields": 0,
                        "field_accuracy": None,
                        "error": str(exc),
                    }
                )
            continue

        base_payload = {
            "source_file": relative_file,
            "voucher_type": voucher_type,
            "ocr_lines": [asdict(line) for line in ocr_lines],
            "tables": [asdict(table) for table in tables],
        }
        stem = Path(relative_file).stem
        _json_dump(output_dir / "ocr" / f"{stem}.json", base_payload)

        if args.ocr_only:
            continue

        for profile in profiles:
            print(f"  [LLM] {profile['name']} -> {profile['model']}")
            try:
                with _override_llm_profile(profile):
                    extraction = extract_document(voucher_type, ocr_lines, tables=tables)
                    validation = validate_extraction(extraction)

                result_dict = extraction.to_dict()
                score = _score_fields(expected_fields, result_dict, voucher_type) if expected_fields else {
                    "matched": 0,
                    "total": 0,
                    "accuracy": None,
                    "details": [],
                }
                summary = _summarize_result(result_dict, validation)
                summary_row = {
                    "file": relative_file,
                    "voucher_type": voucher_type,
                    "profile": profile["name"],
                    "status": summary["status"],
                    "needs_review": summary["needs_review"],
                    "llm_status": summary["llm_status"],
                    "fill_rate": summary["fill_rate"],
                    "filled_fields": summary["filled_fields"],
                    "field_count": summary["field_count"],
                    "review_fields": summary["review_fields"],
                    "items_count": summary["items_count"],
                    "review_items": summary["review_items"],
                    "context_hints_count": summary["context_hints_count"],
                    "warnings_count": summary["warnings_count"],
                    "scored_fields": score["total"],
                    "matched_fields": score["matched"],
                    "field_accuracy": score["accuracy"],
                    "error": "",
                }

                detail_payload = {
                    "profile": profile,
                    "summary": summary_row,
                    "validation": validation,
                    "score": score,
                    "result": result_dict,
                }
                _json_dump(output_dir / "results" / _sanitize_name(profile["name"]) / f"{stem}.json", detail_payload)
            except Exception as exc:  # noqa: BLE001
                summary_row = {
                    "file": relative_file,
                    "voucher_type": voucher_type,
                    "profile": profile["name"],
                    "status": "LLM_FAILED",
                    "needs_review": True,
                    "llm_status": "failed",
                    "fill_rate": 0.0,
                    "filled_fields": 0,
                    "field_count": 0,
                    "review_fields": 0,
                    "items_count": 0,
                    "review_items": 0,
                    "context_hints_count": 0,
                    "warnings_count": 1,
                    "scored_fields": 0,
                    "matched_fields": 0,
                    "field_accuracy": None,
                    "error": str(exc),
                }
                _json_dump(
                    output_dir / "results" / _sanitize_name(profile["name"]) / f"{stem}.json",
                    {"profile": profile, "summary": summary_row, "error": str(exc)},
                )
            rows.append(summary_row)

    _write_summary_files(output_dir, rows)
    print(f"Comparison report written to: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
