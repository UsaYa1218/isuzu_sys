from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]


def _json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _result_path(compare_dir: Path, profile: str, source_file: str) -> Path:
    return compare_dir / "results" / profile / f"{Path(source_file).stem}.json"


def _build_expected_field_draft(result_payload: dict[str, Any]) -> dict[str, Any]:
    fields = (result_payload.get("result") or {}).get("fields") or {}
    expected_fields: dict[str, Any] = {}
    for key, field in fields.items():
        value = field.get("value")
        if value in (None, ""):
            continue
        expected_fields[key] = value
    return expected_fields


def _build_review_metadata(result_payload: dict[str, Any]) -> dict[str, Any]:
    result = result_payload.get("result") or {}
    field_sources = {}
    for key, field in (result.get("fields") or {}).items():
        value = field.get("value")
        if value in (None, ""):
            continue
        field_sources[key] = {
            "value": value,
            "source": field.get("source"),
            "confidence": field.get("confidence"),
            "needs_review": field.get("needs_review"),
        }

    return {
        "llm_status": result.get("llm_status"),
        "warnings": result.get("warnings") or [],
        "context_hints": result.get("context_hints") or [],
        "field_candidates": field_sources,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap expected_fields from a previous llm_compare result directory.")
    parser.add_argument("--compare-dir", type=Path, required=True)
    parser.add_argument("--cases-file", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    compare_dir = args.compare_dir.resolve()
    cases_file = args.cases_file.resolve()
    output_file = args.output_file.resolve()

    payload = _json_load(cases_file)
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("Case file must contain a list or an object with a 'cases' list.")

    bootstrapped_cases = []
    for raw_case in raw_cases:
        case = dict(raw_case)
        source_file = str(case["file"])
        result_path = _result_path(compare_dir, args.profile, source_file)
        if not result_path.exists():
            case.setdefault("expected_fields", {})
            case["review_status"] = "missing_result"
            case["review_notes"] = [f"Result file was not found for profile '{args.profile}'."]
            bootstrapped_cases.append(case)
            continue

        result_payload = _json_load(result_path)
        case["expected_fields"] = _build_expected_field_draft(result_payload)
        case["review_status"] = "needs_human_review"
        case["review_notes"] = [
            "Auto-filled from llm_compare output. Confirm each expected_fields value before scoring.",
            "Remove any wrong value instead of leaving it in place.",
        ]
        case["bootstrap"] = {
            "compare_dir": str(compare_dir),
            "profile": args.profile,
            **_build_review_metadata(result_payload),
        }
        bootstrapped_cases.append(case)

    _json_dump(output_file, {"cases": bootstrapped_cases})
    print(f"Bootstrapped case file written to: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
