from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.main import _build_transfer_records_from_llm, _build_transfer_records_from_tables  # noqa: E402
from app.schemas import ExtractedTable, ExtractionResult  # noqa: E402
from app.services.exporter import export_transfer_summary_xlsx  # noqa: E402


SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def load_cached_extraction(source_path: Path, cache_dir: Path) -> ExtractionResult | None:
    cache_path = cache_dir / f"{source_path.stem}.json"
    if not cache_path.exists():
        return None

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    tables = [ExtractedTable(**table) for table in payload.get("tables") or []]
    raw_text = "\n".join(str(line.get("text") or "") for line in payload.get("ocr_lines") or [])
    return ExtractionResult(
        voucher_type="delivery",
        raw_text=raw_text,
        tables=tables,
    )


def main() -> int:
    input_dir = ROOT_DIR / "runtime" / "testdata"
    cache_dir = ROOT_DIR / "runtime" / "ocr_cache"
    if not input_dir.exists():
        raise FileNotFoundError(f"testdata directory was not found: {input_dir}")

    vouchers = []
    skipped = []
    for source_path in sorted(input_dir.iterdir()):
        if not source_path.is_file() or source_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue

        extraction = load_cached_extraction(source_path, cache_dir)
        if extraction is None:
            skipped.append(source_path.name)
            continue

        records, warnings = _build_transfer_records_from_llm(source_path.name, extraction)
        if not records:
            records = _build_transfer_records_from_tables(source_path.name, extraction)
        vouchers.append(
            {
                "id": source_path.stem,
                "source_filename": source_path.name,
                "status": "TESTDATA",
                "transfer_records": records,
            }
        )
        if warnings:
            print(f"warnings[{source_path.name}]=" + " / ".join(str(warning) for warning in warnings))

    export_path = export_transfer_summary_xlsx(vouchers)
    print(f"exported={export_path}")
    print(f"files={len(vouchers)}")
    print(f"records={sum(len(voucher['transfer_records']) for voucher in vouchers)}")
    if skipped:
        print("skipped_without_cache=" + ",".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
