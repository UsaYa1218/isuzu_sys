from __future__ import annotations

#%%
import json
import inspect
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, UploadFile
from PIL import Image, ImageFilter, ImageOps


HOST = os.environ.get("COLAB_OCR_HOST", "0.0.0.0")
PORT = int(os.environ.get("COLAB_OCR_PORT", "8001"))
SKIP_TUNNEL = os.environ.get("COLAB_OCR_SKIP_TUNNEL", "").strip().lower() in {"1", "true", "yes", "on"}
SERVER_TIMEOUT_SECONDS = int(os.environ.get("COLAB_OCR_SERVER_TIMEOUT", "120"))
TUNNEL_TIMEOUT_SECONDS = int(os.environ.get("COLAB_OCR_TUNNEL_TIMEOUT", "90"))
LOG_DIR = Path(os.environ.get("COLAB_OCR_LOG_DIR", "/content/ocr_runtime_logs"))
BIN_DIR = Path(os.environ.get("COLAB_OCR_BIN_DIR", "/content/bin"))
REMOTE_MAX_SIDE_LIMIT = int(os.environ.get("COLAB_OCR_MAX_SIDE_LIMIT", "5600"))

CLOUDFLARED_BINARY_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
TRYCLOUDFLARE_PATTERN = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")

OCR_TEXT_REPLACEMENTS = (
    ("いすぶ", "いすゞ"),
    ("いすロジスティクス", "いすゞロジスティクス"),
    ("いすぶ自動車", "いすゞ自動車"),
    ("ライネツクス", "ライネックス"),
    ("營業時間", "営業時間"),
    ("输送", "輸送"),
    ("車輛", "車輌"),
    ("才ーダ", "オーダ"),
    ("車台·", "車台・"),
    ("亍", "〒"),
)

temp_dir = Path("/tmp/colab_ocr_runtime")
temp_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TEMP", str(temp_dir))
os.environ.setdefault("TMP", str(temp_dir))
os.environ.setdefault("HF_HOME", str(temp_dir / "hf-home"))
os.environ.setdefault("MODELSCOPE_CACHE", str(temp_dir / "modelscope"))
os.environ.setdefault("XDG_CACHE_HOME", str(temp_dir / "cache"))
tempfile.tempdir = str(temp_dir)


#%%
def run_shell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        check=True,
        text=True,
        capture_output=True,
        executable="/bin/bash",
        env=env,
    )


def ensure_colab_linux() -> None:
    if platform.system() != "Linux":
        raise RuntimeError(
            "This notebook is for Colab/Linux. Switch the VS Code connection to a Colab runtime before executing this file."
        )


def ensure_cloudflared_installed(bin_dir: Path) -> Path:
    existing = shutil.which("cloudflared")
    if existing:
        return Path(existing)
    bin_dir.mkdir(parents=True, exist_ok=True)
    destination = bin_dir / "cloudflared"
    if destination.exists():
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        return destination
    urllib.request.urlretrieve(CLOUDFLARED_BINARY_URL, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    return destination


def ensure_python_dependencies() -> None:
    packages = [
        "fastapi>=0.115,<1.0",
        "uvicorn[standard]>=0.34,<1.0",
        "python-multipart>=0.0.20,<1.0",
        "Pillow>=10.4,<13.0",
        "PyMuPDF>=1.24,<2.0",
        "httpx>=0.28,<1.0",
        "numpy>=1.26,<3.0",
        "paddleocr==3.2.0",
        "langchain<0.3",
    ]
    command = f"{shlex.quote(shutil.which('python') or 'python')} -m pip install -q " + " ".join(shlex.quote(package) for package in packages)
    run_shell(command)


def detect_gpu() -> list[str]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _normalize_ocr_text(text: str) -> str:
    normalized = str(text).strip()
    for src, dest in OCR_TEXT_REPLACEMENTS:
        normalized = normalized.replace(src, dest)
    return normalized


def load_document_images(file_path: Path) -> list[Image.Image]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        import fitz

        document = fitz.open(file_path)
        images: list[Image.Image] = []
        try:
            for page in document:
                pixmap = page.get_pixmap(dpi=300, alpha=False)
                images.append(Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples))
        finally:
            document.close()
        return images

    image = Image.open(file_path)
    return [ImageOps.exif_transpose(image).convert("RGB")]


def preprocess_image(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    enhanced = ImageOps.autocontrast(grayscale)
    if max(enhanced.size) < 4200:
        scale = min(1.5, REMOTE_MAX_SIDE_LIMIT / max(enhanced.size))
        if scale > 1.0:
            enhanced = enhanced.resize(
                (max(1, int(enhanced.width * scale)), max(1, int(enhanced.height * scale))),
                Image.Resampling.LANCZOS,
            )
    enhanced = enhanced.filter(ImageFilter.MedianFilter(size=3))
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.6, percent=180, threshold=2))
    return enhanced.convert("RGB")


@lru_cache(maxsize=1)
def get_ocr_engine() -> Any:
    from paddleocr import PaddleOCR

    try:
        return PaddleOCR(
            lang="japan",
            device="gpu",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_det_limit_side_len=REMOTE_MAX_SIDE_LIMIT,
            text_det_limit_type="max",
        )
    except TypeError:
        return PaddleOCR(
            use_angle_cls=True,
            lang="japan",
            show_log=False,
            use_gpu=True,
            enable_mkldnn=False,
            ir_optim=False,
            cpu_threads=2,
            det_limit_side_len=REMOTE_MAX_SIDE_LIMIT,
            det_limit_type="max",
        )


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


def _build_lines_from_predict(results: list[Any], fallback_page: int) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for result in results:
        payload = _coerce_json_payload(result)
        data = payload.get("res", payload)
        texts = data.get("rec_texts") or ([data.get("rec_text")] if data.get("rec_text") else [])
        scores = data.get("rec_scores") or ([data.get("rec_score")] if data.get("rec_score") else [])
        polygons = data.get("rec_polys") or data.get("dt_polys") or []
        page = int(data.get("page_index") or fallback_page)

        for index, text in enumerate(texts):
            if not text:
                continue
            polygon = polygons[index] if index < len(polygons) else [[0, 0], [0, 0], [0, 0], [0, 0]]
            score = float(scores[index]) if index < len(scores) else 0.0
            lines.append(
                {
                    "text": _normalize_ocr_text(str(text)),
                    "confidence": score,
                    "bbox": [[float(x), float(y)] for x, y in polygon],
                    "page": page,
                }
            )
    return lines


def _build_lines_from_legacy_ocr_result(raw_result: Any, fallback_page: int) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    page_lines = _unwrap_result(raw_result)
    for entry in page_lines or []:
        try:
            bbox = [[float(x), float(y)] for x, y in entry[0]]
            text = str(entry[1][0]).strip()
            confidence = float(entry[1][1])
        except (IndexError, TypeError, ValueError):
            continue
        if not text:
            continue
        lines.append(
            {
                "text": _normalize_ocr_text(text),
                "confidence": confidence,
                "bbox": bbox,
                "page": fallback_page,
            }
        )
    return lines


def _uses_modern_predict_api(engine: Any) -> bool:
    predict = getattr(engine, "predict", None)
    if not callable(predict):
        return False
    try:
        parameters = inspect.signature(predict).parameters
    except (TypeError, ValueError):
        return False
    return "use_doc_orientation_classify" in parameters


def run_ocr(file_path: Path) -> list[dict[str, Any]]:
    engine = get_ocr_engine()
    use_modern_api = _uses_modern_predict_api(engine)
    all_lines: list[dict[str, Any]] = []
    for page_index, image in enumerate(load_document_images(file_path), start=1):
        prepared = preprocess_image(image)
        prepared_array = np.array(prepared)
        if hasattr(engine, "predict"):
            try:
                results = engine.predict(prepared_array)
                predicted_lines = _build_lines_from_predict(list(results), fallback_page=page_index)
                if predicted_lines:
                    all_lines.extend(predicted_lines)
                    continue
            except Exception:
                pass

        try:
            if use_modern_api:
                result = engine.ocr(prepared_array)
            else:
                result = engine.ocr(prepared_array, cls=True)
        except TypeError as exc:
            if "unexpected keyword argument 'cls'" not in str(exc):
                raise
            result = engine.ocr(prepared_array)

        predicted_lines = _build_lines_from_predict(list(result), fallback_page=page_index)
        if predicted_lines:
            all_lines.extend(predicted_lines)
            continue

        all_lines.extend(_build_lines_from_legacy_ocr_result(result, fallback_page=page_index))
    return sorted(
        all_lines,
        key=lambda line: (
            line["page"],
            min(point[1] for point in line["bbox"]),
            min(point[0] for point in line["bbox"]),
        ),
    )


def build_ocr_app() -> FastAPI:
    app = FastAPI(title="Colab OCR Worker")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/ocr")
    async def ocr(file: UploadFile = File(...)) -> dict[str, object]:
        suffix = Path(file.filename or "upload.bin").suffix or ".bin"
        temp_upload_dir = Path("/tmp/colab_ocr_uploads")
        temp_upload_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_upload_dir / f"{int(time.time() * 1000)}{suffix}"
        temp_path.write_bytes(await file.read())
        try:
            return {"ocr_lines": run_ocr(temp_path)}
        finally:
            temp_path.unlink(missing_ok=True)

    return app


def wait_for_http_ready(url: str, timeout_seconds: int) -> None:
    import httpx

    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"Server did not become ready: {last_error}")


def start_tunnel(cloudflared_path: Path, target_url: str, log_path: Path) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [str(cloudflared_path), "tunnel", "--url", target_url, "--no-autoupdate"],
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + TUNNEL_TIMEOUT_SECONDS
    public_url = ""
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            match = TRYCLOUDFLARE_PATTERN.search(text)
            if match:
                public_url = match.group(0)
                break
        time.sleep(1)
    if not public_url:
        process.terminate()
        raise TimeoutError("Cloudflare tunnel URL was not detected in time.")
    return process, public_url


def start_ocr_server_thread(host: str, port: int) -> threading.Thread:
    import uvicorn

    app = build_ocr_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


#%%
def start_ocr_colab(
    host: str = HOST,
    port: int = PORT,
    skip_tunnel: bool = SKIP_TUNNEL,
    server_timeout_seconds: int = SERVER_TIMEOUT_SECONDS,
    tunnel_timeout_seconds: int = TUNNEL_TIMEOUT_SECONDS,
    log_dir: Path = LOG_DIR,
    bin_dir: Path = BIN_DIR,
) -> dict[str, object]:
    global TUNNEL_TIMEOUT_SECONDS
    TUNNEL_TIMEOUT_SECONDS = tunnel_timeout_seconds

    ensure_colab_linux()
    log_dir.mkdir(parents=True, exist_ok=True)
    ensure_python_dependencies()

    local_base_url = f"http://127.0.0.1:{port}"
    server_thread = start_ocr_server_thread(host, port)
    wait_for_http_ready(f"{local_base_url}/health", server_timeout_seconds)

    public_base_url = ""
    tunnel_process = None
    tunnel_log = log_dir / "cloudflared.log"
    if not skip_tunnel:
        cloudflared_path = ensure_cloudflared_installed(bin_dir)
        tunnel_process, public_base_url = start_tunnel(cloudflared_path, local_base_url, tunnel_log)

    return {
        "gpu": detect_gpu(),
        "local_base_url": local_base_url,
        "public_base_url": public_base_url,
        "ocr_server_thread_alive": server_thread.is_alive(),
        "cloudflared_pid": tunnel_process.pid if tunnel_process else None,
        "cloudflared_log": str(tunnel_log) if tunnel_process else None,
        "next_env": {
            "REMOTE_OCR_BASE_URL": public_base_url,
        },
    }


#%%
if __name__ == "__main__":
    result = start_ocr_colab()
    print(json.dumps(result, ensure_ascii=False, indent=2))
