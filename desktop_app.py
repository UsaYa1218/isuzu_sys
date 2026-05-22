from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Y, filedialog, messagebox, ttk
import tkinter as tk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # noqa: BLE001
    DND_FILES = None
    TkinterDnD = None

from app.main import _build_transfer_records_from_llm, _build_transfer_records_from_tables
from app.schemas import ExtractionResult
from app.services.exporter import export_transfer_summary_xlsx
from app.config import settings
from app.services.ocr_pipeline import _get_gpu_ocr_engine, extract_tables, run_ocr


SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


class TransferSummaryDesktop:
    def __init__(self) -> None:
        root_class = TkinterDnD.Tk if TkinterDnD is not None else tk.Tk
        self.root = root_class()
        self.root.title("伝票自動転記ツール")
        self.root.geometry("980x640")
        self.root.minsize(780, 520)

        self.files: list[Path] = []
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.processing = False
        self.last_export_path: Path | None = None
        self.started_at: float | None = None
        self.current_file_index = 0
        self.total_files = 0

        self._build_ui()
        self._poll_messages()
        self._run_first_startup_check()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="伝票自動転記ツール", font=("", 18, "bold")).grid(row=0, column=0, sticky=W)
        ttk.Label(
            header,
            text="PDF/画像を追加して、車型・車番、搬出入日時、搬出入場所だけを抽出します。",
        ).grid(row=1, column=0, sticky=W, pady=(6, 0))

        actions = ttk.Frame(self.root, padding=(18, 8))
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure(4, weight=1)
        ttk.Button(actions, text="ファイル追加", command=self.add_files).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="選択を削除", command=self.remove_selected).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(actions, text="すべて削除", command=self.clear_files).grid(row=0, column=2, padx=(0, 8))
        self.process_button = ttk.Button(actions, text="これらのファイルを処理", command=self.process_files)
        self.process_button.grid(row=0, column=3, padx=(0, 8))
        ttk.Button(actions, text="出力フォルダを開く", command=self.open_export_folder).grid(row=0, column=5)

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=8)

        file_frame = ttk.Frame(body, padding=1)
        file_frame.columnconfigure(0, weight=1)
        file_frame.rowconfigure(1, weight=1)
        ttk.Label(file_frame, text="処理対象ファイル").grid(row=0, column=0, sticky=W, pady=(0, 6))
        self.file_list = tk.Listbox(file_frame, selectmode=tk.EXTENDED)
        self.file_list.grid(row=1, column=0, sticky="nsew")
        file_scroll = ttk.Scrollbar(file_frame, orient=VERTICAL, command=self.file_list.yview)
        file_scroll.grid(row=1, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=file_scroll.set)
        self.drop_hint = ttk.Label(
            file_frame,
            text="ここにPDF/画像をドラッグ&ドロップできます。" if DND_FILES else "ドラッグ&ドロップを使うには tkinterdnd2 が必要です。",
        )
        self.drop_hint.grid(row=2, column=0, columnspan=2, sticky=W, pady=(8, 0))
        if DND_FILES:
            self.file_list.drop_target_register(DND_FILES)
            self.file_list.dnd_bind("<<Drop>>", self._on_drop)

        log_frame = ttk.Frame(body, padding=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="処理ログ").grid(row=0, column=0, sticky=W, pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        body.add(file_frame, weight=3)
        body.add(log_frame, weight=2)

        footer = ttk.Frame(self.root, padding=(18, 8, 18, 16))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(footer, text="待機中")
        self.status_label.grid(row=0, column=0, sticky=W)
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.eta_label = ttk.Label(footer, text="残り時間: -")
        self.eta_label.grid(row=2, column=0, sticky=W, pady=(6, 0))

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="PDF/画像を選択",
            filetypes=[
                ("PDF/画像", "*.pdf *.png *.jpg *.jpeg *.webp"),
                ("PDF", "*.pdf"),
                ("画像", "*.png *.jpg *.jpeg *.webp"),
                ("すべて", "*.*"),
            ],
        )
        self._add_paths([Path(path) for path in paths])

    def _on_drop(self, event: object) -> None:
        raw_data = getattr(event, "data", "")
        paths = [Path(path) for path in self.root.tk.splitlist(raw_data)]
        self._add_paths(paths)

    def _add_paths(self, paths: list[Path]) -> None:
        added = 0
        existing = {path.resolve() for path in self.files}
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in existing:
                continue
            self.files.append(resolved)
            existing.add(resolved)
            self.file_list.insert(END, str(resolved))
            added += 1
        self._set_status(f"{len(self.files)}件のファイルが選択されています。")
        if added == 0 and paths:
            messagebox.showinfo("追加なし", "対応しているPDF/画像ファイルは追加されませんでした。")

    def remove_selected(self) -> None:
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)
            del self.files[index]
        self._set_status(f"{len(self.files)}件のファイルが選択されています。")

    def clear_files(self) -> None:
        self.files.clear()
        self.file_list.delete(0, END)
        self._set_status("待機中")

    def process_files(self) -> None:
        if self.processing:
            return
        if not self.files:
            messagebox.showinfo("ファイル未選択", "処理するPDF/画像を追加してください。")
            return
        self.processing = True
        self.started_at = time.monotonic()
        self.current_file_index = 0
        self.total_files = len(self.files)
        self.process_button.configure(state="disabled")
        self.progress.configure(maximum=100, value=0)
        self.eta_label.configure(text="残り時間: 計算中")
        self._clear_log()
        worker = threading.Thread(target=self._process_worker, args=(list(self.files),), daemon=True)
        worker.start()

    def _process_worker(self, files: list[Path]) -> None:
        vouchers = []
        total_records = 0
        try:
            for index, source_path in enumerate(files, start=1):
                self.messages.put(("file_start", f"{index}|{len(files)}|{source_path.name}"))
                self.messages.put(("status", f"{source_path.name} ファイル処理中 0%"))
                self.messages.put(("log", f"[{index}/{len(files)}] {source_path.name}"))
                ocr_lines = run_ocr(
                    source_path,
                    progress_callback=lambda message, percent, file_index=index, total=len(files): self._queue_ocr_progress(
                        file_index,
                        total,
                        message,
                        percent,
                    ),
                )
                self.messages.put(("status", f"{source_path.name} 表抽出中 90%"))
                tables = extract_tables(source_path, ocr_lines)
                self.messages.put(("status", f"{source_path.name} LLM抽出中 95%"))
                extraction = ExtractionResult(
                    voucher_type="delivery",
                    raw_text="\n".join(line.text for line in ocr_lines),
                    tables=tables,
                )
                records, warnings = _build_transfer_records_from_llm(source_path.name, extraction)
                if not records:
                    records = _build_transfer_records_from_tables(source_path.name, extraction)
                total_records += len(records)
                vouchers.append(
                    {
                        "id": source_path.stem,
                        "source_filename": source_path.name,
                        "status": "DESKTOP",
                        "transfer_records": records,
                    }
                )
                self.messages.put(("log", f"  抽出行: {len(records)}件"))
                for warning in warnings:
                    self.messages.put(("log", f"  注意: {warning}"))
                self.messages.put(("overall_progress", f"{index}|{len(files)}|100"))

            export_path = export_transfer_summary_xlsx(vouchers)
            self.last_export_path = export_path
            self.messages.put(("done", f"完了: {len(files)}ファイル / {total_records}行\n{export_path}"))
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self._set_status(value)
            elif kind == "file_start":
                index_text, total_text, filename = value.split("|", 2)
                self.current_file_index = int(index_text)
                self.total_files = int(total_text)
                self._append_log(f"{filename} ファイル処理中 0%")
                self._update_overall_progress(self.current_file_index - 1, self.total_files, 0)
            elif kind == "log":
                self._append_log(value)
            elif kind == "ocr_progress":
                index_text, total_text, percent_text, message = value.split("|", 3)
                index = int(index_text)
                total = int(total_text)
                percent = float(percent_text)
                self._set_status(message)
                self._append_log(message)
                self._update_overall_progress(index - 1, total, percent)
            elif kind == "overall_progress":
                index_text, total_text, percent_text = value.split("|", 2)
                self._update_overall_progress(int(index_text) - 1, int(total_text), float(percent_text))
            elif kind == "done":
                self.processing = False
                self.process_button.configure(state="normal")
                self.progress.configure(value=100)
                self.eta_label.configure(text="残り時間: 0秒")
                self._set_status(value.splitlines()[0])
                self._append_log(value)
                messagebox.showinfo("Excel作成完了", value)
            elif kind == "error":
                self.processing = False
                self.process_button.configure(state="normal")
                self.eta_label.configure(text="残り時間: -")
                self._set_status("エラーが発生しました")
                self._append_log(value)
                messagebox.showerror("処理エラー", value)
        self.root.after(150, self._poll_messages)

    def _queue_ocr_progress(self, file_index: int, total_files: int, message: str, percent: float | None) -> None:
        if percent is None:
            self.messages.put(("log", message))
            return
        bounded = max(0.0, min(100.0, float(percent)))
        self.messages.put(("ocr_progress", f"{file_index}|{total_files}|{bounded}|{message}"))

    def _update_overall_progress(self, completed_before_current: int, total_files: int, current_file_percent: float) -> None:
        if total_files <= 0:
            self.progress.configure(value=0)
            self.eta_label.configure(text="残り時間: -")
            return
        overall = ((completed_before_current + current_file_percent / 100.0) / total_files) * 100
        overall = max(0.0, min(100.0, overall))
        self.progress.configure(value=overall)
        self.eta_label.configure(text=f"残り時間: {self._estimate_remaining(overall)}")

    def _estimate_remaining(self, overall_percent: float) -> str:
        if not self.started_at or overall_percent <= 0:
            return "計算中"
        elapsed = time.monotonic() - self.started_at
        remaining = elapsed * (100.0 - overall_percent) / overall_percent
        if remaining < 60:
            return f"約{int(max(1, remaining))}秒"
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        return f"約{minutes}分{seconds:02d}秒"

    def _run_first_startup_check(self) -> None:
        marker = settings.data_dir / ".desktop_initialized"
        if marker.exists():
            return
        self._append_log("初回起動: 必要フォルダとOCR環境を確認しています。")
        worker = threading.Thread(target=self._first_startup_worker, args=(marker,), daemon=True)
        worker.start()

    def _first_startup_worker(self, marker: Path) -> None:
        try:
            settings.ensure_directories()
            if settings.paddleocr_use_gpu:
                self._prepare_gpu_runtime_packages()
                self.messages.put(("log", "初回起動: GPU OCRエンジンを初期化しています。"))
                _get_gpu_ocr_engine()
                self.messages.put(("log", "初回起動: GPU OCRエンジンの初期化が完了しました。"))
            else:
                self.messages.put(("log", "初回起動: GPU設定は無効です。"))
            if shutil.which("ollama") and settings.ollama_model:
                self.messages.put(("log", f"初回起動: LLMモデルを確認しています: {settings.ollama_model}"))
                subprocess.run(
                    ["ollama", "pull", settings.ollama_model],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.messages.put(("log", "初回起動: LLMモデル確認が完了しました。"))
            elif settings.ollama_base_url.startswith("http://127.0.0.1") or settings.ollama_base_url.startswith("http://localhost"):
                self.messages.put(("log", "初回起動: Ollamaが見つかりません。LLM抽出にはOllamaのインストールが必要です。"))
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("log", f"初回起動: OCR環境確認でエラー: {type(exc).__name__}: {exc}"))
            self.messages.put(("log", "GPU用ライブラリやCUDAが未導入の場合は、READMEの手順でセットアップしてください。"))

    def _prepare_gpu_runtime_packages(self) -> None:
        gpu_info = self._detect_nvidia_gpu()
        if not gpu_info:
            self.messages.put(("log", "初回起動: NVIDIA GPUが見つかりません。CPU OCRへフォールバックします。"))
            return

        cuda_version = gpu_info.get("cuda_version")
        self.messages.put(("log", f"初回起動: NVIDIA GPU検出: {gpu_info.get('name') or 'GPU'} / CUDA {cuda_version or '不明'}"))
        package_index = self._paddle_gpu_index_for_cuda(cuda_version)
        if not package_index:
            self.messages.put(("log", "初回起動: 対応CUDAが判定できません。CPU OCRへフォールバックします。"))
            return

        if self._runtime_package_has_gpu_paddle():
            self.messages.put(("log", "初回起動: GPU版PaddlePaddleは導入済みです。"))
            return

        python_cmd = self._find_python_command()
        if not python_cmd:
            self.messages.put(("log", "初回起動: Pythonが見つからないためGPUライブラリを自動導入できません。"))
            self.messages.put(("log", "初回起動: Python 3.10系をインストール後、再起動してください。"))
            return

        settings.runtime_package_dir.mkdir(parents=True, exist_ok=True)
        install_cmd = [
            *python_cmd,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--target",
            str(settings.runtime_package_dir),
            "paddlepaddle-gpu==3.2.0",
            "-i",
            package_index,
        ]
        self.messages.put(("log", f"初回起動: GPUライブラリをダウンロードしています: {package_index}"))
        result = subprocess.run(install_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            self.messages.put(("log", "初回起動: GPUライブラリの自動導入に失敗しました。CPU OCRへフォールバックします。"))
            self.messages.put(("log", result.stdout[-1200:] if result.stdout else "pip install failed"))
            return
        if str(settings.runtime_package_dir) not in sys.path:
            sys.path.insert(0, str(settings.runtime_package_dir))
        self.messages.put(("log", "初回起動: GPUライブラリの導入が完了しました。"))

    def _detect_nvidia_gpu(self) -> dict[str, str] | None:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return None
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=name,driver_version,cuda_version", "--format=csv,noheader"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except Exception:  # noqa: BLE001
            return None
        first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
        if not first_line.strip():
            return None
        parts = [part.strip() for part in first_line.split(",")]
        return {
            "name": parts[0] if len(parts) > 0 else "",
            "driver_version": parts[1] if len(parts) > 1 else "",
            "cuda_version": parts[2] if len(parts) > 2 else "",
        }

    def _paddle_gpu_index_for_cuda(self, cuda_version: str | None) -> str | None:
        if not cuda_version:
            return "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
        try:
            major_text, minor_text, *_ = cuda_version.split(".")
            version = float(f"{int(major_text)}.{int(minor_text)}")
        except (ValueError, TypeError):
            return "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
        if version >= 12.6:
            return "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
        if version >= 11.8:
            return "https://www.paddlepaddle.org.cn/packages/stable/cu118/"
        return None

    def _runtime_package_has_gpu_paddle(self) -> bool:
        paddle_dir = settings.runtime_package_dir / "paddle"
        dist_infos = list(settings.runtime_package_dir.glob("paddlepaddle_gpu-*.dist-info"))
        return paddle_dir.exists() and bool(dist_infos)

    def _find_python_command(self) -> list[str] | None:
        python_exe = shutil.which("python")
        if python_exe:
            return [python_exe]
        py_launcher = shutil.which("py")
        if py_launcher:
            return [py_launcher, "-3"]
        return None

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, text + "\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", END)
        self.log_text.configure(state="disabled")

    def open_export_folder(self) -> None:
        folder = self.last_export_path.parent if self.last_export_path else Path("runtime/exports").resolve()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # type: ignore[attr-defined]

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--smoke-ocr":
        raise SystemExit(run_smoke_ocr(Path(sys.argv[2])))
    app = TransferSummaryDesktop()
    app.run()


def run_smoke_ocr(source_path: Path) -> int:
    settings.ensure_directories()
    log_path = settings.data_dir / "smoke_ocr.log"
    lines: list[str] = []

    def log(message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{timestamp} {message}")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def progress(message: str, percent: float | None) -> None:
        if percent is None:
            log(message)
        else:
            log(f"{message} [{percent:.1f}%]")

    try:
        source_path = source_path.resolve()
        log(f"smoke start: {source_path}")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        ocr_lines = run_ocr(source_path, progress_callback=progress)
        log(f"ocr lines: {len(ocr_lines)}")
        tables = extract_tables(source_path, ocr_lines)
        log(f"tables: {len(tables)}")
        extraction = ExtractionResult(
            voucher_type="delivery",
            raw_text="\n".join(line.text for line in ocr_lines),
            tables=tables,
        )
        records, warnings = _build_transfer_records_from_llm(source_path.name, extraction)
        if not records:
            records = _build_transfer_records_from_tables(source_path.name, extraction)
        log(f"records: {len(records)}")
        for warning in warnings:
            log(f"warning: {warning}")

        export_path = export_transfer_summary_xlsx(
            [
                {
                    "id": source_path.stem,
                    "source_filename": source_path.name,
                    "status": "SMOKE",
                    "transfer_records": records,
                }
            ]
        )
        log(f"export: {export_path}")
        log("smoke ok")
        return 0
    except Exception:  # noqa: BLE001
        log("smoke failed")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    main()
