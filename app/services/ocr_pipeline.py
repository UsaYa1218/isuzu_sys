from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

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


@lru_cache(maxsize=1)
def _get_ocr_engine() -> Any:
    from paddleocr import PaddleOCR

    try:
        return PaddleOCR(
            lang=settings.paddleocr_lang,
            device="cpu",
            text_detection_model_dir=str(settings.paddleocr_model_dir / "PP-OCRv5_server_det"),
            text_recognition_model_dir=str(settings.paddleocr_model_dir / "PP-OCRv5_server_rec"),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        return PaddleOCR(
            use_angle_cls=True,
            lang=settings.paddleocr_lang,
            show_log=False,
            use_gpu=settings.paddleocr_use_gpu,
            enable_mkldnn=False,
            ir_optim=False,
            cpu_threads=2,
            det_model_dir=str(settings.paddleocr_model_dir / "det"),
            rec_model_dir=str(settings.paddleocr_model_dir / "rec"),
            cls_model_dir=str(settings.paddleocr_model_dir / "cls"),
        )


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
                headers = padded_rows[0] if padded_rows else []
                rows = padded_rows[1:] if len(padded_rows) > 1 else []
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


def extract_tables(file_path: Path, ocr_lines: list[OCRLine] | None = None) -> list[ExtractedTable]:
    vector_tables = _collect_vector_tables(file_path)
    if vector_tables:
        return vector_tables

    images = load_document_images(file_path)
    if ocr_lines is None:
        ocr_lines = run_ocr(file_path)
    return _collect_grid_tables(images, ocr_lines)


def preprocess_image(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    return ImageOps.autocontrast(grayscale).convert("RGB")


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
                    text=str(text).strip(),
                    confidence=score,
                    bbox=[[float(x), float(y)] for x, y in polygon],
                    page=page,
                )
            )
    return lines


def run_ocr(file_path: Path) -> list[OCRLine]:
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
                    text=text,
                    confidence=confidence,
                    bbox=bbox,
                    page=page_index,
                )
            )
    return sorted(all_lines, key=lambda line: (line.page, round(line.center_y, 1), line.left))
