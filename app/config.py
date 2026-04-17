from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")


@dataclass(slots=True)
class Settings:
    app_env: str
    app_name: str
    base_dir: Path
    data_dir: Path
    database_path: Path
    upload_dir: Path
    export_dir: Path
    templates_path: Path
    static_dir: Path
    ocr_dpi: int
    ocr_confidence_threshold: float
    paddleocr_lang: str
    paddleocr_use_gpu: bool
    paddleocr_model_dir: Path
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = BASE_DIR / os.getenv("DATA_DIR", "runtime")
        database_path = BASE_DIR / os.getenv("DATABASE_PATH", "runtime/app.db")
        upload_dir = BASE_DIR / os.getenv("UPLOAD_DIR", "runtime/uploads")
        export_dir = BASE_DIR / os.getenv("EXPORT_DIR", "runtime/exports")
        settings = cls(
            app_env=os.getenv("APP_ENV", "local"),
            app_name=os.getenv("APP_NAME", "Voucher Auto Transfer Tool"),
            base_dir=BASE_DIR,
            data_dir=data_dir,
            database_path=database_path,
            upload_dir=upload_dir,
            export_dir=export_dir,
            templates_path=BASE_DIR / "app" / "templates",
            static_dir=BASE_DIR / "app" / "static",
            ocr_dpi=int(os.getenv("OCR_DPI", "300")),
            ocr_confidence_threshold=float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.75")),
            paddleocr_lang=os.getenv("PADDLEOCR_LANG", "japan"),
            paddleocr_use_gpu=_to_bool(os.getenv("PADDLEOCR_USE_GPU"), False),
            paddleocr_model_dir=data_dir / "paddleocr",
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            ollama_timeout_seconds=int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")),
        )
        settings.ensure_directories()
        return settings

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.export_dir, self.paddleocr_model_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings.from_env()
