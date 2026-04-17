from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ..config import settings
from ..schemas import ExtractedTable, OCRLine


logger = logging.getLogger(__name__)
os.environ.setdefault("FLAGS_use_mkldnn", "0")
temp_dir = settings.data_dir / "tmp"
temp_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TEMP", str(temp_dir))
os.environ.setdefault("TMP", str(temp_dir))
os.environ.setdefault("HF_HOME", str(settings.data_dir / "hf-home"))
os.environ.setdefault("MODELSCOPE_CACHE", str(settings.data_dir / "modelscope"))
os.environ.setdefault("XDG_CACHE_HOME", str(settings.data_dir / "cache"))
tempfile.tempdir = str(temp_dir)

OCR_TEXT_REPLACEMENTS = (
    ("输送", "輸送"),
    ("いすぶ", "いすゞ"),
    ("いすロジスティクス", "いすゞロジスティクス"),
    ("いすぶ自動車", "いすゞ自動車"),
    ("ライネツクス", "ライネックス"),
    ("營業時間", "営業時間"),
    ("車輛", "車輌"),
    ("才ーダ", "オーダ"),
    ("車台·", "車台・"),
    ("內線", "内線"),
    ("亍", "〒"),
)


def _existing_model_path(name: str) -> str | None:
    path = settings.paddleocr_model_dir / name
    if path.exists():
        return str(path)
    logger.warning("PaddleOCR local model directory was not found: %s", path)
    return None


@lru_cache(maxsize=1)
def _get_ocr_engine() -> Any:
    from paddleocr import PaddleOCR

    try:
        kwargs: dict[str, Any] = {
            "lang": settings.paddleocr_lang,
            "device": "cpu",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_det_limit_side_len": settings.paddleocr_max_side_limit,
            "text_det_limit_type": "max",
        }
        det_model_dir = _existing_model_path("PP-OCRv5_server_det")
        rec_model_dir = _existing_model_path("PP-OCRv5_server_rec")
        if det_model_dir:
            kwargs["text_detection_model_dir"] = det_model_dir
        if rec_model_dir:
            kwargs["text_recognition_model_dir"] = rec_model_dir

        return PaddleOCR(
            **kwargs,
        )
    except TypeError:
        kwargs: dict[str, Any] = {
            "use_angle_cls": True,
            "lang": settings.paddleocr_lang,
            "show_log": False,
            "use_gpu": settings.paddleocr_use_gpu,
            "enable_mkldnn": False,
            "ir_optim": False,
            "cpu_threads": 2,
            "det_limit_side_len": settings.paddleocr_max_side_limit,
            "det_limit_type": "max",
        }
        det_model_dir = _existing_model_path("det")
        rec_model_dir = _existing_model_path("rec")
        cls_model_dir = _existing_model_path("cls")
        if det_model_dir:
            kwargs["det_model_dir"] = det_model_dir
        if rec_model_dir:
            kwargs["rec_model_dir"] = rec_model_dir
        if cls_model_dir:
            kwargs["cls_model_dir"] = cls_model_dir
        return PaddleOCR(**kwargs)


def _render_pdf(pdf_path: Path) -> list[Image.Image]:
    import fitz

    document = fitz.open(pdf_path)
    images: list[Image.Image] = []
    for page in document:
        pixmap = page.get_pixmap(dpi=settings.ocr_dpi, alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        images.append(image)
    document.close()
    return images


def load_document_images(file_path: Path) -> list[Image.Image]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _render_pdf(file_path)
    image = Image.open(file_path)
    return [ImageOps.exif_transpose(image).convert("RGB")]


def _normalize_table_cell(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").split())


def _normalize_ocr_text(text: str) -> str:
    normalized = str(text).strip()
    for src, dest in OCR_TEXT_REPLACEMENTS:
        normalized = normalized.replace(src, dest)
    return normalized


def _table_row_non_empty_count(row: list[str]) -> int:
    return sum(1 for cell in row if _normalize_table_cell(cell))


def _select_header_row(rows: list[list[str]]) -> int:
    if not rows:
        return 0

    hints = ("no", "no.", "数量", "単価", "金額", "摘要", "品名", "車種", "型式", "登録番号", "状態")
    best_index = 0
    best_score = float("-inf")
    for index, row in enumerate(rows[: min(8, len(rows))]):
        normalized = [_normalize_table_cell(cell) for cell in row]
        non_empty_count = sum(1 for cell in normalized if cell)
        if non_empty_count < 2:
            continue

        text_lengths = [len(cell) for cell in normalized if cell]
        longest = max(text_lengths, default=0)
        total = sum(text_lengths)
        joined = " ".join(normalized).lower()
        hint_score = sum(1 for hint in hints if hint in joined)
        score = (non_empty_count * 5) + (hint_score * 4) - (longest * 0.08) - (total * 0.02) - index
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def _looks_like_sparse_label_table(headers: list[str], rows: list[list[str]]) -> bool:
    if len(headers) > 2 or not rows:
        return False

    normalized_headers = [_normalize_table_cell(header) for header in headers]
    if not any(normalized_headers):
        return False

    column_non_empty = [0] * len(headers)
    for row in rows:
        for index in range(min(len(headers), len(row))):
            if _normalize_table_cell(row[index]):
                column_non_empty[index] += 1

    return sum(1 for count in column_non_empty if count == 0) >= 1


def _coerce_page_number(raw_page: Any, fallback_page: int) -> int:
    if raw_page in (None, "", 0):
        return fallback_page
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return fallback_page
    return page if page > 0 else fallback_page


def _line_bounds(line: OCRLine) -> tuple[float, float, float, float]:
    xs = [point[0] for point in line.bbox]
    ys = [point[1] for point in line.bbox]
    return min(xs), min(ys), max(xs), max(ys)


def _normalize_match_text(text: str) -> str:
    return "".join(str(text).replace("\u3000", " ").split()).lower()


def _group_consecutive(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    groups: list[tuple[int, int]] = []
    start = prev = indices[0]
    for index in indices[1:]:
        if index == prev + 1:
            prev = index
            continue
        groups.append((start, prev))
        start = prev = index
    groups.append((start, prev))
    return groups


def _band_center(band: tuple[int, int]) -> float:
    start, end = band
    return (start + end) / 2.0


def _collect_vector_tables(file_path: Path) -> list[ExtractedTable]:
    if file_path.suffix.lower() != ".pdf":
        return []

    import fitz

    tables: list[ExtractedTable] = []
    document = fitz.open(file_path)
    try:
        for page_index, page in enumerate(document, start=1):
            if not hasattr(page, "find_tables"):
                continue
            finder = page.find_tables()
            for table_index, table in enumerate(getattr(finder, "tables", []), start=1):
                extracted = table.extract() or []
                normalized_rows = [
                    [_normalize_table_cell(cell) for cell in row]
                    for row in extracted
                ]
                normalized_rows = [row for row in normalized_rows if any(cell for cell in row)]
                if not normalized_rows:
                    continue

                width = max(len(row) for row in normalized_rows)
                padded_rows = [row + [""] * (width - len(row)) for row in normalized_rows]
                header_index = _select_header_row(padded_rows)
                headers = padded_rows[header_index] if padded_rows else []
                rows = padded_rows[header_index + 1 :] if len(padded_rows) > (header_index + 1) else []
                rows = [row for row in rows if _table_row_non_empty_count(row) > 0]
                if _looks_like_sparse_label_table(headers, rows):
                    continue
                bbox = [float(value) for value in getattr(table, "bbox", (0, 0, 0, 0))]

                tables.append(
                    ExtractedTable(
                        page=page_index,
                        table_index=table_index,
                        bbox=bbox,
                        headers=headers,
                        rows=rows,
                    )
                )
    finally:
        document.close()

    return tables


def _detect_table_bands(image: Image.Image) -> list[tuple[list[float], list[float]]]:
    grayscale = np.array(image.convert("L"))
    binary = grayscale < 180

    row_scores = binary.mean(axis=1)
    row_threshold = max(0.18, float(row_scores.max()) * 0.45)
    row_groups = [
        group
        for group in _group_consecutive(np.flatnonzero(row_scores >= row_threshold).tolist())
        if (group[1] - group[0] + 1) >= 2
    ]
    if len(row_groups) < 3:
        return []

    sequences: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = [row_groups[0]]
    gap_threshold = max(int(image.height * 0.1), 120)
    for group in row_groups[1:]:
        previous_center = _band_center(current[-1])
        current_center = _band_center(group)
        if current_center - previous_center <= gap_threshold:
            current.append(group)
            continue
        if len(current) >= 3:
            sequences.append(current)
        current = [group]
    if len(current) >= 3:
        sequences.append(current)

    table_bands: list[tuple[list[float], list[float]]] = []
    for sequence in sequences:
        top = max(sequence[0][0] - 2, 0)
        bottom = min(sequence[-1][1] + 2, binary.shape[0] - 1)
        if bottom - top < max(int(image.height * 0.08), 80):
            continue

        region = binary[top : bottom + 1, :]
        col_scores = region.mean(axis=0)
        col_threshold = max(0.3, float(col_scores.max()) * 0.45)
        column_groups = [
            group
            for group in _group_consecutive(np.flatnonzero(col_scores >= col_threshold).tolist())
            if (group[1] - group[0] + 1) >= 2
        ]
        if len(column_groups) < 3:
            continue

        left = column_groups[0][0]
        right = column_groups[-1][1]
        if right - left < image.width * 0.4:
            continue

        horizontal_lines = [_band_center(group) for group in sequence]
        vertical_lines = [_band_center(group) for group in column_groups]
        table_bands.append((horizontal_lines, vertical_lines))

    return table_bands


def _merge_cell_text(entries: list[tuple[float, float, str]]) -> str:
    if not entries:
        return ""
    ordered = sorted(entries, key=lambda item: (item[0], item[1], item[2]))
    unique_texts: list[str] = []
    seen: set[str] = set()
    for _, _, text in ordered:
        normalized = _normalize_table_cell(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_texts.append(normalized)
    return "\n".join(unique_texts)


def _grid_table_from_lines(
    page_index: int,
    table_index: int,
    horizontal_lines: list[float],
    vertical_lines: list[float],
    page_lines: list[OCRLine],
) -> ExtractedTable | None:
    if len(horizontal_lines) < 2 or len(vertical_lines) < 2:
        return None

    row_count = len(horizontal_lines) - 1
    column_count = len(vertical_lines) - 1
    cells: list[list[list[tuple[float, float, str]]]] = [
        [[] for _ in range(column_count)]
        for _ in range(row_count)
    ]

    top_bound = horizontal_lines[0]
    bottom_bound = horizontal_lines[-1]
    left_bound = vertical_lines[0]
    right_bound = vertical_lines[-1]

    for line in page_lines:
        line_left, line_top, line_right, line_bottom = _line_bounds(line)
        center_x = (line_left + line_right) / 2.0
        center_y = (line_top + line_bottom) / 2.0
        if center_x < left_bound or center_x > right_bound or center_y < top_bound or center_y > bottom_bound:
            continue

        row_index = next(
            (index for index in range(row_count) if horizontal_lines[index] <= center_y <= horizontal_lines[index + 1]),
            None,
        )
        column_index = next(
            (index for index in range(column_count) if vertical_lines[index] <= center_x <= vertical_lines[index + 1]),
            None,
        )
        if row_index is None or column_index is None:
            continue
        cells[row_index][column_index].append((line_top, line_left, line.text))

    table_rows = [
        [_merge_cell_text(cell_entries) for cell_entries in row]
        for row in cells
    ]
    non_empty_row_indexes = [
        index
        for index, row in enumerate(table_rows)
        if any(cell for cell in row)
    ]
    if len(non_empty_row_indexes) < 2:
        return None

    header_index = non_empty_row_indexes[0]
    headers = table_rows[header_index]
    rows = [
        table_rows[index]
        for index in non_empty_row_indexes[1:]
        if any(table_rows[index])
    ]
    if not headers or not any(headers) or not rows:
        return None

    return ExtractedTable(
        page=page_index,
        table_index=table_index,
        bbox=[
            float(left_bound),
            float(top_bound),
            float(right_bound),
            float(bottom_bound),
        ],
        headers=headers,
        rows=rows,
    )


def _collect_grid_tables(images: list[Image.Image], ocr_lines: list[OCRLine]) -> list[ExtractedTable]:
    lines_by_page: dict[int, list[OCRLine]] = defaultdict(list)
    for line in ocr_lines:
        lines_by_page[line.page].append(line)

    tables: list[ExtractedTable] = []
    for page_index, image in enumerate(images, start=1):
        page_lines = lines_by_page.get(page_index, [])
        if not page_lines:
            continue

        table_bands = _detect_table_bands(image)
        if not table_bands:
            continue

        page_tables: list[ExtractedTable] = []
        for table_index, (horizontal_lines, vertical_lines) in enumerate(table_bands, start=1):
            table = _grid_table_from_lines(page_index, table_index, horizontal_lines, vertical_lines, page_lines)
            if table is not None:
                page_tables.append(table)

        tables.extend(page_tables)

    return tables


def _find_best_matching_line(lines: list[OCRLine], target: str) -> OCRLine | None:
    normalized_target = _normalize_match_text(target)
    candidates = [
        line
        for line in lines
        if normalized_target in _normalize_match_text(line.text)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda line: (line.page, line.top, line.left))
    return candidates[0]


def _extract_inline_or_nearby_value(label_line: OCRLine, lines: list[OCRLine], label_hint: str | None = None) -> str:
    label_text = _normalize_table_cell(label_line.text)
    compact_label = "".join((label_hint or label_text).split())
    compact_text = "".join(_normalize_table_cell(label_line.text).split())
    if compact_label and compact_text.startswith(compact_label):
        tail = compact_text[len(compact_label) :].lstrip(":：")
        if tail:
            return tail

    candidates: list[tuple[float, OCRLine]] = []
    for line in lines:
        if line is label_line:
            continue
        same_row = abs(line.center_y - label_line.center_y) <= 35 and line.left > label_line.left
        if same_row:
            candidates.append((line.left - label_line.left, line))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return _normalize_table_cell(candidates[0][1].text)


def _find_text_by_hint(
    lines: list[OCRLine],
    hint: str,
    *,
    min_left: float | None = None,
    max_left: float | None = None,
    y_range: tuple[float, float] | None = None,
) -> OCRLine | None:
    normalized_hint = _normalize_match_text(hint)
    matches = []
    for line in lines:
        normalized = _normalize_match_text(line.text)
        if normalized_hint not in normalized:
            continue
        if min_left is not None and line.left < min_left:
            continue
        if max_left is not None and line.left > max_left:
            continue
        if y_range is not None and not (y_range[0] <= line.center_y <= y_range[1]):
            continue
        matches.append(line)
    if not matches:
        return None
    matches.sort(key=lambda line: (line.center_y, line.left))
    return matches[0]


def _find_all_text_by_hint(
    lines: list[OCRLine],
    hint: str,
    *,
    min_left: float | None = None,
    max_left: float | None = None,
    y_range: tuple[float, float] | None = None,
) -> list[OCRLine]:
    normalized_hint = _normalize_match_text(hint)
    matches: list[OCRLine] = []
    for line in lines:
        normalized = _normalize_match_text(line.text)
        if normalized_hint not in normalized:
            continue
        if min_left is not None and line.left < min_left:
            continue
        if max_left is not None and line.left > max_left:
            continue
        if y_range is not None and not (y_range[0] <= line.center_y <= y_range[1]):
            continue
        matches.append(line)
    matches.sort(key=lambda line: (line.center_y, line.left))
    return matches


def _join_unique_texts(texts: list[str], separator: str = " ") -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = _normalize_table_cell(text)
        compact = _normalize_match_text(normalized)
        if not normalized or compact in seen:
            continue
        seen.add(compact)
        merged.append(normalized)
    return separator.join(merged)


def _collect_band_text(
    lines: list[OCRLine],
    *,
    min_left: float,
    max_left: float,
    min_top: float,
    max_top: float,
    exclude: set[str] | None = None,
) -> str:
    excluded = {_normalize_match_text(text) for text in (exclude or set())}
    texts = [
        line.text
        for line in sorted(lines, key=lambda item: (item.top, item.left))
        if min_left <= line.left <= max_left
        and min_top <= line.top <= max_top
        and _normalize_match_text(line.text) not in excluded
    ]
    return _join_unique_texts(texts)


def _extract_value_below(label_line: OCRLine | None, lines: list[OCRLine], *, x_padding: float = 180, y_padding: float = 170) -> str:
    if label_line is None:
        return ""
    return _collect_band_text(
        lines,
        min_left=max(label_line.left - x_padding, 0),
        max_left=label_line.left + x_padding + 260,
        min_top=label_line.top + 20,
        max_top=label_line.top + y_padding,
        exclude={label_line.text},
    )


def _extract_transport_section_table(ocr_lines: list[OCRLine]) -> list[ExtractedTable]:
    if not ocr_lines:
        return []

    row_labels = ["日時", "拠点名", "住所", "ご担当者様", "連絡先", "備考"]
    column_targets = ["発地", "経由地", "着地"]
    tables: list[ExtractedTable] = []

    pages = sorted({line.page for line in ocr_lines})
    for page in pages:
        page_lines = [line for line in ocr_lines if line.page == page]
        title_line = _find_best_matching_line(page_lines, "輸送区間")
        row_lines = {label: _find_best_matching_line(page_lines, label) for label in row_labels}
        column_lines = {label: _find_best_matching_line(page_lines, label) for label in column_targets}

        if title_line is None:
            continue
        if sum(line is not None for line in row_lines.values()) < 3:
            continue
        if sum(line is not None for line in column_lines.values()) < 2:
            continue

        column_centers = {
            label: line.center_x
            for label, line in column_lines.items()
            if line is not None
        }
        if len(column_centers) < 2:
            continue

        headers = ["項目"] + column_targets
        ignored_texts = {_normalize_match_text("輸送区間"), *(_normalize_match_text(label) for label in row_labels), *(_normalize_match_text(label) for label in column_targets)}
        transport_rows: list[list[str]] = []
        involved_lines: list[OCRLine] = [title_line]

        for label in row_labels:
            row_line = row_lines.get(label)
            if row_line is None:
                continue
            involved_lines.append(row_line)
            row_values = {column: "" for column in column_targets}
            for line in page_lines:
                normalized = _normalize_match_text(line.text)
                if normalized in ignored_texts:
                    continue
                if abs(line.center_y - row_line.center_y) > 60:
                    continue
                if line.center_x < min(column_centers.values()) - 250:
                    continue

                nearest_column = min(column_centers.items(), key=lambda item: abs(line.center_x - item[1]))[0]
                current = row_values[nearest_column]
                text = _normalize_table_cell(line.text)
                if not text:
                    continue
                row_values[nearest_column] = f"{current} {text}".strip() if current else text
                involved_lines.append(line)

            transport_rows.append([label, row_values.get("発地", ""), row_values.get("経由地", ""), row_values.get("着地", "")])

        if not any(any(cell for cell in row[1:]) for row in transport_rows):
            continue

        left = min(line.left for line in involved_lines)
        top = min(line.top for line in involved_lines)
        right = max(_line_bounds(line)[2] for line in involved_lines)
        bottom = max(_line_bounds(line)[3] for line in involved_lines)
        tables.append(
            ExtractedTable(
                page=page,
                table_index=0,
                bbox=[float(left), float(top), float(right), float(bottom)],
                headers=headers,
                rows=transport_rows,
            )
        )

    return tables


def _extract_transport_request_tables(ocr_lines: list[OCRLine]) -> list[ExtractedTable]:
    if not ocr_lines:
        return []

    tables: list[ExtractedTable] = []
    pages = sorted({line.page for line in ocr_lines})
    for page in pages:
        page_lines = [line for line in ocr_lines if line.page == page]
        title_line = _find_best_matching_line(page_lines, "搬送依頼")
        origin_label = _find_best_matching_line(page_lines, "出発地")
        destination_label = _find_best_matching_line(page_lines, "搬送先")
        pickup_label = _find_best_matching_line(page_lines, "引き取り日")
        delivery_label = _find_best_matching_line(page_lines, "搬入希望日")

        if title_line and origin_label and destination_label:
            origin_value = _extract_inline_or_nearby_value(origin_label, page_lines, "出発地")
            destination_value = _extract_inline_or_nearby_value(destination_label, page_lines, "搬送先")
            pickup_value = _extract_inline_or_nearby_value(pickup_label, page_lines, "引き取り日") if pickup_label else ""
            delivery_value = _extract_inline_or_nearby_value(delivery_label, page_lines, "搬入希望日") if delivery_label else ""
            origin_no = _find_text_by_hint(page_lines, "(1)", min_left=origin_label.left, y_range=(origin_label.center_y + 20, origin_label.center_y + 120))
            destination_no = _find_text_by_hint(page_lines, "(2)", min_left=destination_label.left, y_range=(destination_label.center_y + 20, destination_label.center_y + 120))
            billing_anchor = _find_text_by_hint(page_lines, "請求先", min_left=1500)
            billing_note = ""
            if billing_anchor:
                billing_lines = [
                    _normalize_table_cell(line.text)
                    for line in page_lines
                    if line.left >= billing_anchor.left
                    and billing_anchor.center_y - 20 <= line.center_y <= billing_anchor.center_y + 140
                    and _normalize_match_text(line.text) not in {_normalize_match_text("請求先")}
                ]
                billing_note = " ".join(dict.fromkeys([line for line in billing_lines if line]))

            involved = [line for line in [title_line, origin_label, destination_label, pickup_label, delivery_label, origin_no, destination_no, billing_anchor] if line is not None]
            rows = [
                ["拠点", origin_value, destination_value],
                ["識別", _normalize_table_cell(origin_no.text) if origin_no else "", _normalize_table_cell(destination_no.text) if destination_no else ""],
                ["希望日", pickup_value, delivery_value],
                ["請求先メモ", "", billing_note],
            ]
            if any(any(cell for cell in row[1:]) for row in rows):
                left = min(line.left for line in involved)
                top = min(line.top for line in involved)
                right = max(_line_bounds(line)[2] for line in involved)
                bottom = max(_line_bounds(line)[3] for line in involved)
                tables.append(
                    ExtractedTable(
                        page=page,
                        table_index=0,
                        bbox=[float(left), float(top), float(right), float(bottom)],
                        headers=["項目", "出発地", "搬送先"],
                        rows=rows,
                    )
                )

        left_block = _find_text_by_hint(page_lines, "引取場所")
        right_block = _find_text_by_hint(page_lines, "納入場所")
        if left_block and right_block:
            mid_x = (left_block.left + right_block.left) / 2
            left_title = _find_text_by_hint(page_lines, "藤沢工場", max_left=mid_x, y_range=(left_block.center_y, left_block.center_y + 120))
            right_title = _find_text_by_hint(page_lines, "港栄作業株式会社", y_range=(right_block.center_y, right_block.center_y + 120))
            left_address = _find_text_by_hint(page_lines, "住所", max_left=mid_x, y_range=(left_block.center_y + 80, left_block.center_y + 220))
            right_address = _find_text_by_hint(page_lines, "住所", min_left=mid_x, y_range=(right_block.center_y + 80, right_block.center_y + 220))
            left_contact = _find_text_by_hint(page_lines, "担当", max_left=mid_x, y_range=(left_block.center_y + 140, left_block.center_y + 320))
            right_contact = _find_text_by_hint(page_lines, "担当", min_left=mid_x, y_range=(right_block.center_y + 140, right_block.center_y + 320))
            left_tel = _find_text_by_hint(page_lines, "TEL", max_left=mid_x, y_range=(left_block.center_y + 220, left_block.center_y + 420))
            right_tel = _find_text_by_hint(page_lines, "TEL", min_left=mid_x, y_range=(right_block.center_y + 220, right_block.center_y + 420))

            def _collect_value(anchor: OCRLine | None, min_x: float, max_x: float, y_padding: float = 22, label_hint: str | None = None) -> str:
                if anchor is None:
                    return ""
                inline_value = _extract_inline_or_nearby_value(anchor, page_lines, label_hint=label_hint)
                if inline_value:
                    return inline_value
                texts = [
                    _normalize_table_cell(line.text)
                    for line in page_lines
                    if min_x <= line.left <= max_x
                    and anchor.center_y - y_padding <= line.center_y <= anchor.center_y + y_padding
                    and line is not anchor
                ]
                return " ".join(dict.fromkeys([text for text in texts if text]))

            left_name = _normalize_table_cell(left_title.text) if left_title else ""
            right_name = _normalize_table_cell(right_title.text) if right_title else ""
            left_address_value = _collect_value(left_address, 0, mid_x, label_hint="住所")
            right_address_value = _collect_value(right_address, mid_x, 99999, label_hint="住所")
            left_contact_value = _collect_value(left_contact, 0, mid_x, label_hint="担当")
            right_contact_value = _collect_value(right_contact, mid_x, 99999, label_hint="担当")
            left_tel_value = _collect_value(left_tel, 0, mid_x, y_padding=30, label_hint="TEL")
            right_tel_value = _collect_value(right_tel, mid_x, 99999, y_padding=30, label_hint="TEL")

            involved = [line for line in [left_block, right_block, left_title, right_title, left_address, right_address, left_contact, right_contact, left_tel, right_tel] if line is not None]
            rows = [
                ["名称", left_name, right_name],
                ["住所", left_address_value, right_address_value],
                ["担当", left_contact_value, right_contact_value],
                ["TEL", left_tel_value, right_tel_value],
            ]
            if any(any(cell for cell in row[1:]) for row in rows):
                left = min(line.left for line in involved)
                top = min(line.top for line in involved)
                right = max(_line_bounds(line)[2] for line in involved)
                bottom = max(_line_bounds(line)[3] for line in involved)
                tables.append(
                    ExtractedTable(
                        page=page,
                        table_index=0,
                        bbox=[float(left), float(top), float(right), float(bottom)],
                        headers=["項目", "引取場所", "納入場所"],
                        rows=rows,
                    )
                )

    return tables


def _extract_isuzu_dispatch_tables(ocr_lines: list[OCRLine]) -> list[ExtractedTable]:
    if not ocr_lines:
        return []

    tables: list[ExtractedTable] = []
    pages = sorted({line.page for line in ocr_lines})
    for page in pages:
        page_lines = [line for line in ocr_lines if line.page == page]
        title_line = _find_best_matching_line(page_lines, "運搬依頼書")
        from_line = _find_best_matching_line(page_lines, "FROM")
        to_line = _find_best_matching_line(page_lines, "TO")
        vehicle_header = _find_best_matching_line(page_lines, "車台・CARNO.")
        if not (title_line and from_line and to_line and vehicle_header):
            continue

        recipient_candidates = [
            line
            for line in page_lines
            if line.top < from_line.top
            and line.left < 1600
            and "殿" in _normalize_table_cell(line.text)
        ]
        recipient_line = recipient_candidates[0] if recipient_candidates else None

        dispatch_text = _collect_band_text(
            page_lines,
            min_left=3400,
            max_left=5200,
            min_top=440,
            max_top=580,
            exclude={"担当者", "TEL"},
        )
        schedule_line = _find_best_matching_line(page_lines, "輸送予定日")
        load_no_line = _find_best_matching_line(page_lines, "積載No.")
        trip_line = _find_best_matching_line(page_lines, "便数")
        person_line = _find_best_matching_line(page_lines, "担当者")
        tel_line = _find_text_by_hint(page_lines, "TEL", min_left=3400, y_range=(540, 760))
        fax_line = _find_text_by_hint(page_lines, "FAX")

        trip_candidates = [
            line.text
            for line in page_lines
            if trip_line is not None
            and trip_line.top + 20 <= line.top <= from_line.top + 30
            and 1200 <= line.left <= 2200
            and any(character.isdigit() for character in _normalize_table_cell(line.text))
        ]
        trip_value = _join_unique_texts(trip_candidates) or (_extract_inline_or_nearby_value(trip_line, page_lines, "便数") if trip_line else "") or _extract_value_below(trip_line, page_lines)

        dispatch_rows = [
            ["依頼先", _normalize_table_cell(recipient_line.text) if recipient_line else ""],
            ["配車担当", dispatch_text],
            ["輸送予定日", _extract_inline_or_nearby_value(schedule_line, page_lines, "輸送予定日") if schedule_line else ""],
            ["積載No.", _extract_inline_or_nearby_value(load_no_line, page_lines, "積載No.") if load_no_line else ""],
            ["便数", trip_value],
            ["担当者", _extract_inline_or_nearby_value(person_line, page_lines, "担当者") if person_line else ""],
            ["TEL", _extract_inline_or_nearby_value(tel_line, page_lines, "TEL") if tel_line else ""],
            ["FAX", _normalize_table_cell(fax_line.text).replace("FAX", "").strip(" :：") if fax_line else ""],
        ]
        dispatch_rows = [row for row in dispatch_rows if row[1]]
        if dispatch_rows:
            involved = [
                line
                for line in [title_line, recipient_line, schedule_line, load_no_line, trip_line, person_line, tel_line, fax_line]
                if line is not None
            ]
            if involved:
                tables.append(
                    ExtractedTable(
                        page=page,
                        table_index=0,
                        bbox=[
                            float(min(line.left for line in involved)),
                            float(min(line.top for line in involved)),
                            float(max(_line_bounds(line)[2] for line in involved)),
                            float(max(_line_bounds(line)[3] for line in involved)),
                        ],
                        title="依頼情報",
                        headers=["項目", "値"],
                        rows=dispatch_rows,
                    )
                )

        section_top = from_line.top - 40
        section_bottom = vehicle_header.top - 70
        left_name_label = _find_text_by_hint(page_lines, "名称", max_left=500, y_range=(section_top, section_bottom))
        right_name_label = _find_text_by_hint(page_lines, "名称", min_left=2500, y_range=(section_top, section_bottom))
        left_address_label = _find_text_by_hint(page_lines, "住所", max_left=500, y_range=(section_top, section_bottom))
        right_address_label = _find_text_by_hint(page_lines, "住所", min_left=2500, y_range=(section_top, section_bottom))
        left_tel_label = _find_text_by_hint(page_lines, "TEL", max_left=500, y_range=(section_top, section_bottom))
        right_tel_label = _find_text_by_hint(page_lines, "ＴEＬ", min_left=2500, y_range=(section_top, section_bottom)) or _find_text_by_hint(page_lines, "TEL", min_left=2500, y_range=(section_top, section_bottom))

        location_rows: list[list[str]] = []
        involved_location_lines: list[OCRLine] = [from_line, to_line]
        if left_name_label and right_name_label:
            name_left = _collect_band_text(
                page_lines,
                min_left=500,
                max_left=2600,
                min_top=left_name_label.top - 35,
                max_top=left_name_label.top + 80,
                exclude={"名称"},
            )
            name_right = _collect_band_text(
                page_lines,
                min_left=3000,
                max_left=5200,
                min_top=right_name_label.top - 35,
                max_top=right_name_label.top + 80,
                exclude={"名称"},
            )
            if name_left or name_right:
                location_rows.append(["名称", name_left, name_right])
                involved_location_lines.extend([left_name_label, right_name_label])

            address_left = _collect_band_text(
                page_lines,
                min_left=500,
                max_left=2600,
                min_top=from_line.top + 140,
                max_top=from_line.top + 280,
                exclude={"住所"},
            )
            address_right = _collect_band_text(
                page_lines,
                min_left=3000,
                max_left=5200,
                min_top=to_line.top + 140,
                max_top=to_line.top + 280,
                exclude={"住所"},
            )
            if address_left or address_right:
                location_rows.append(["住所", address_left, address_right])
                if left_address_label is not None:
                    involved_location_lines.append(left_address_label)
                if right_address_label is not None:
                    involved_location_lines.append(right_address_label)

        if left_tel_label and right_tel_label:
            tel_left = _collect_band_text(
                page_lines,
                min_left=500,
                max_left=2600,
                min_top=left_tel_label.top - 35,
                max_top=left_tel_label.top + 80,
                exclude={"TEL"},
            )
            tel_right = _collect_band_text(
                page_lines,
                min_left=3000,
                max_left=5200,
                min_top=right_tel_label.top - 35,
                max_top=right_tel_label.top + 80,
                exclude={"ＴEＬ", "TEL"},
            )
            if tel_left or tel_right:
                location_rows.append(["TEL", tel_left, tel_right])
                involved_location_lines.extend([left_tel_label, right_tel_label])

        if location_rows:
            tables.append(
                ExtractedTable(
                    page=page,
                    table_index=0,
                    bbox=[
                        float(min(line.left for line in involved_location_lines)),
                        float(min(line.top for line in involved_location_lines)),
                        float(max(_line_bounds(line)[2] for line in involved_location_lines)),
                        float(max(_line_bounds(line)[3] for line in involved_location_lines)),
                    ],
                    title="発着地情報",
                    headers=["項目", "出発地(FROM)", "搬送先(TO)"],
                    rows=location_rows,
                )
            )

        number_lines = [
            line
            for line in page_lines
            if line.top >= vehicle_header.top + 120
            and line.left <= 360
            and _normalize_table_cell(line.text).isdigit()
        ]
        number_lines.sort(key=lambda line: line.top)
        vehicle_rows: list[list[str]] = []
        involved_vehicle_lines: list[OCRLine] = [vehicle_header]
        for index, number_line in enumerate(number_lines):
            top = number_line.top - 45
            bottom = number_lines[index + 1].top - 45 if index + 1 < len(number_lines) else number_line.top + 180
            row_lines = [line for line in page_lines if top <= line.top <= bottom]
            row = [
                _collect_band_text(row_lines, min_left=0, max_left=340, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=340, max_left=650, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=650, max_left=950, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=950, max_left=1700, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=1700, max_left=2250, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=2250, max_left=2600, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=2600, max_left=3500, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=3500, max_left=3950, min_top=top, max_top=bottom),
                _collect_band_text(row_lines, min_left=3950, max_left=5200, min_top=top, max_top=bottom),
            ]
            vehicle_name = row[4]
            pos_no = row[5]
            order_no = row[6]
            if (
                vehicle_name
                and pos_no
                and re.fullmatch(r"[A-Z]{1,3}", pos_no)
                and re.search(r"\d{6,}", order_no)
            ):
                row[4] = f"{vehicle_name} {pos_no}"
                row[5] = ""
            if not any(row[1:]):
                continue
            vehicle_rows.append(row)
            involved_vehicle_lines.extend(row_lines)

        if vehicle_rows:
            tables.append(
                ExtractedTable(
                    page=page,
                    table_index=0,
                    bbox=[
                        float(min(line.left for line in involved_vehicle_lines)),
                        float(min(line.top for line in involved_vehicle_lines)),
                        float(max(_line_bounds(line)[2] for line in involved_vehicle_lines)),
                        float(max(_line_bounds(line)[3] for line in involved_vehicle_lines)),
                    ],
                    title="車両一覧",
                    headers=["No.", "区分", "型式", "車台番号", "車輌名称", "POSNo.", "オーダーNo.", "納期", "備考"],
                    rows=vehicle_rows,
                )
            )

    return tables


def extract_tables(file_path: Path, ocr_lines: list[OCRLine] | None = None) -> list[ExtractedTable]:
    if ocr_lines is None:
        ocr_lines = run_ocr(file_path)

    vector_tables = _collect_vector_tables(file_path)
    semantic_tables = _extract_transport_section_table(ocr_lines)
    transport_request_tables = _extract_transport_request_tables(ocr_lines)
    dispatch_request_tables = _extract_isuzu_dispatch_tables(ocr_lines)
    semantic_pages = {table.page for table in dispatch_request_tables}
    if semantic_pages:
        vector_tables = [table for table in vector_tables if table.page not in semantic_pages]
    if vector_tables:
        tables = vector_tables + semantic_tables + transport_request_tables + dispatch_request_tables
    else:
        images = load_document_images(file_path)
        grid_tables = _collect_grid_tables(images, ocr_lines)
        if semantic_pages:
            grid_tables = [table for table in grid_tables if table.page not in semantic_pages]
        tables = grid_tables + semantic_tables + transport_request_tables + dispatch_request_tables

    tables.sort(key=lambda table: (table.page, table.bbox[1] if len(table.bbox) > 1 else 0, table.bbox[0] if table.bbox else 0))
    for index, table in enumerate(tables, start=1):
        table.table_index = index
    return tables


def preprocess_image(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    enhanced = ImageOps.autocontrast(grayscale)
    if max(enhanced.size) < 4200:
        scale = min(1.5, settings.paddleocr_max_side_limit / max(enhanced.size))
        if scale > 1.0:
            enhanced = enhanced.resize(
                (max(1, int(enhanced.width * scale)), max(1, int(enhanced.height * scale))),
                Image.Resampling.LANCZOS,
            )
    enhanced = enhanced.filter(ImageFilter.MedianFilter(size=3))
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.6, percent=180, threshold=2))
    return enhanced.convert("RGB")


def _unwrap_result(raw_result: Any) -> list[Any]:
    if not raw_result:
        return []
    if isinstance(raw_result, list) and raw_result and isinstance(raw_result[0], list):
        first = raw_result[0]
        if first and isinstance(first[0], list) and len(first[0]) == 2 and isinstance(first[0][1], tuple):
            return first
    return raw_result


def _coerce_json_payload(result: Any) -> dict[str, Any]:
    if hasattr(result, "json"):
        payload = result.json
        if isinstance(payload, str):
            return json.loads(payload)
        if isinstance(payload, dict):
            return payload
    if isinstance(result, dict):
        return result
    return {}


def _build_lines_from_predict(results: list[Any], fallback_page: int) -> list[OCRLine]:
    lines: list[OCRLine] = []
    for result in results:
        payload = _coerce_json_payload(result)
        data = payload.get("res", payload)
        texts = data.get("rec_texts") or ([data.get("rec_text")] if data.get("rec_text") else [])
        scores = data.get("rec_scores") or ([data.get("rec_score")] if data.get("rec_score") else [])
        polygons = data.get("rec_polys") or data.get("dt_polys") or []
        page = _coerce_page_number(data.get("page_index"), fallback_page)

        for index, text in enumerate(texts):
            if not text:
                continue
            polygon = polygons[index] if index < len(polygons) else [[0, 0], [0, 0], [0, 0], [0, 0]]
            score = float(scores[index]) if index < len(scores) else 0.0
            lines.append(
                OCRLine(
                    text=_normalize_ocr_text(str(text)),
                    confidence=score,
                    bbox=[[float(x), float(y)] for x, y in polygon],
                    page=page,
                )
            )
    return lines


def _run_remote_ocr(file_path: Path) -> list[OCRLine]:
    url = f"{settings.remote_ocr_base_url.rstrip('/')}/ocr"
    with file_path.open("rb") as stream:
        response = httpx.post(
            url,
            files={"file": (file_path.name, stream, "application/octet-stream")},
            timeout=settings.remote_ocr_timeout_seconds,
        )
    response.raise_for_status()
    payload = response.json()
    raw_lines = payload.get("ocr_lines") or []
    return [OCRLine(**line) for line in raw_lines]


def run_ocr(file_path: Path) -> list[OCRLine]:
    if settings.remote_ocr_base_url.strip():
        try:
            return _run_remote_ocr(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Remote OCR failed, fallback to local OCR: %s", exc)

    engine = _get_ocr_engine()
    all_lines: list[OCRLine] = []
    for page_index, image in enumerate(load_document_images(file_path), start=1):
        prepared = preprocess_image(image)
        if hasattr(engine, "predict"):
            try:
                results = engine.predict(np.array(prepared))
                predicted_lines = _build_lines_from_predict(list(results), fallback_page=page_index)
                if predicted_lines:
                    all_lines.extend(predicted_lines)
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("PaddleOCR predict() failed, fallback to ocr(): %s", exc)

        result = engine.ocr(np.array(prepared), cls=True)
        page_lines = _unwrap_result(result)
        for entry in page_lines or []:
            try:
                bbox = [[float(x), float(y)] for x, y in entry[0]]
                text = str(entry[1][0]).strip()
                confidence = float(entry[1][1])
            except (IndexError, TypeError, ValueError):
                logger.warning("Unexpected OCR entry skipped: %s", entry)
                continue
            if not text:
                continue
            all_lines.append(
                OCRLine(
                    text=_normalize_ocr_text(text),
                    confidence=confidence,
                    bbox=bbox,
                    page=page_index,
                )
            )
    return sorted(all_lines, key=lambda line: (line.page, round(line.center_y, 1), line.left))
