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
from typing import Any
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Y, filedialog, messagebox, ttk
import tkinter as tk

from PIL import Image, ImageTk

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
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.processing = False
        self.last_export_path: Path | None = None
        self.started_at: float | None = None
        self.current_file_index = 0
        self.total_files = 0
        self.validation_confirmation_event: threading.Event | None = None
        self.validation_confirmed = False
        self.validation_dialog: tk.Toplevel | None = None

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

            review_records = [
                record
                for voucher in vouchers
                for record in voucher.get("transfer_records", [])
                if record.get("needs_review")
            ]
            if review_records or total_records == 0:
                confidence_values = [
                    float(record.get("confidence") or 0.0)
                    for record in review_records
                ]
                minimum_confidence = min(confidence_values) if confidence_values else None
                score_text = f"{minimum_confidence * 100:.1f}%" if minimum_confidence is not None else "-"
                self.validation_confirmed = False
                self.validation_confirmation_event = threading.Event()
                self.messages.put(
                    (
                        "validation_required",
                        {
                            "vouchers": vouchers,
                            "files": files,
                            "score_text": score_text,
                            "threshold_text": f"{settings.ocr_confidence_threshold * 100:.1f}%",
                            "review_count": len(review_records),
                            "total_records": total_records,
                        },
                    )
                )
                self.validation_confirmation_event.wait()
                if not self.validation_confirmed:
                    self.messages.put(("cancelled", "Validation の確認が完了しなかったため、Excel 出力を中止しました。"))
                    return

            total_records = sum(len(voucher.get("transfer_records", [])) for voucher in vouchers)
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
            elif kind == "validation_required":
                self._open_validation_review_dialog(value)
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
            elif kind == "cancelled":
                self.processing = False
                self.process_button.configure(state="normal")
                self.eta_label.configure(text="残り時間: -")
                self._set_status("確認未完了のため出力を中止しました")
                self._append_log(value)
        self.root.after(150, self._poll_messages)

    def _open_validation_review_dialog(self, payload: dict[str, Any]) -> None:
        if self.validation_dialog is not None and self.validation_dialog.winfo_exists():
            self.validation_dialog.lift()
            return

        vouchers = payload["vouchers"]
        source_paths = list(payload["files"])
        review_rows = self._flatten_review_rows(vouchers)
        if not review_rows:
            review_rows.append(self._empty_review_row(vouchers, 0))

        dialog = tk.Toplevel(self.root)
        self.validation_dialog = dialog
        dialog.title("Validation 確認 - 抽出内容の確認・修正")
        dialog.geometry("1480x820")
        dialog.minsize(1120, 660)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        header = ttk.Frame(dialog, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Validation 確認が必要です", font=("", 17, "bold")).grid(row=0, column=0, sticky=W)
        if payload["total_records"] == 0:
            result_summary = "陸送情報が抽出されませんでした。元伝票を確認して、必要な行を追加してください。"
        else:
            result_summary = (
                f"確認対象: {payload['review_count']}行 / "
                f"最低信頼度: {payload['score_text']} / 基準: {payload['threshold_text']}"
            )
        ttk.Label(header, text=result_summary).grid(row=1, column=0, sticky=W, pady=(8, 0))
        ttk.Label(
            header,
            text="元ファイルと照合し、誤りや未記載項目を修正してから「確認済みとして Excel 出力」を押してください。",
        ).grid(row=2, column=0, sticky=W, pady=(6, 0))

        content = ttk.PanedWindow(dialog, orient=tk.HORIZONTAL)
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=8)

        preview_frame = ttk.LabelFrame(content, text="元伝票プレビュー", padding=10)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)
        preview_controls = ttk.Frame(preview_frame)
        preview_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        preview_status = ttk.Label(preview_controls, text="表示するファイルを選択してください")
        preview_status.pack(side=LEFT, fill=X, expand=True)
        preview_canvas = tk.Canvas(preview_frame, background="#f2f2f2", highlightthickness=0)
        preview_canvas.grid(row=1, column=0, sticky="nsew")
        preview_v_scroll = ttk.Scrollbar(preview_frame, orient=VERTICAL, command=preview_canvas.yview)
        preview_v_scroll.grid(row=1, column=1, sticky="ns")
        preview_h_scroll = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=preview_canvas.xview)
        preview_h_scroll.grid(row=2, column=0, sticky="ew")
        preview_canvas.configure(yscrollcommand=preview_v_scroll.set, xscrollcommand=preview_h_scroll.set)
        content.add(preview_frame, weight=4)

        review_frame = ttk.Frame(content, padding=1)
        review_frame.columnconfigure(0, weight=1)
        review_frame.rowconfigure(0, weight=1)
        review_content = ttk.PanedWindow(review_frame, orient=tk.VERTICAL)
        review_content.grid(row=0, column=0, sticky="nsew")

        list_frame = ttk.Frame(review_content, padding=1)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)
        list_actions = ttk.Frame(list_frame)
        list_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(list_actions, text="抽出結果一覧", font=("", 11, "bold")).pack(side=LEFT)
        columns = (
            "source_filename",
            "vehicle_model",
            "vehicle_number",
            "pickup_datetime",
            "pickup_location",
            "delivery_datetime",
            "delivery_location",
            "confidence",
        )
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse", height=9)
        headings = {
            "source_filename": "ファイル名",
            "vehicle_model": "車種",
            "vehicle_number": "車番/VIN",
            "pickup_datetime": "搬出日時",
            "pickup_location": "搬出場所",
            "delivery_datetime": "搬入日時",
            "delivery_location": "搬入場所",
            "confidence": "信頼度",
        }
        widths = {
            "source_filename": 150,
            "vehicle_model": 120,
            "vehicle_number": 120,
            "pickup_datetime": 125,
            "pickup_location": 170,
            "delivery_datetime": 125,
            "delivery_location": 170,
            "confidence": 70,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=70, stretch=column in {"pickup_location", "delivery_location"})
        tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=tree.yview)
        tree_scroll.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=tree_scroll.set)
        review_content.add(list_frame, weight=3)

        edit_frame = ttk.LabelFrame(review_content, text="選択行の確認・修正", padding=14)
        for column in range(4):
            edit_frame.columnconfigure(column, weight=1)
        entries: dict[str, ttk.Entry] = {}
        entry_fields = [
            ("vehicle_model", "車種"),
            ("vehicle_number", "車番/VIN"),
            ("pickup_datetime", "搬出日時"),
            ("pickup_location", "搬出場所"),
            ("delivery_datetime", "搬入日時"),
            ("delivery_location", "搬入場所"),
            ("notes", "備考"),
        ]
        for index, (field, label_text) in enumerate(entry_fields):
            row_index = (index // 4) * 2
            column_index = index % 4
            ttk.Label(edit_frame, text=label_text).grid(row=row_index, column=column_index, sticky=W, padx=(0, 10), pady=(0, 4))
            entry = ttk.Entry(edit_frame)
            entry.grid(row=row_index + 1, column=column_index, sticky="ew", padx=(0, 10), pady=(0, 10))
            entries[field] = entry
        review_content.add(edit_frame, weight=2)
        content.add(review_frame, weight=5)

        selected_index: list[int | None] = [None]
        preview_path: list[Path | None] = [None]
        preview_page: list[int] = [0]
        preview_zoom: list[float] = [0.85]
        preview_photo: list[ImageTk.PhotoImage | None] = [None]

        def render_preview(reset_page: bool = False) -> None:
            path = preview_path[0]
            if path is None or not path.exists():
                preview_canvas.delete("all")
                preview_photo[0] = None
                preview_status.configure(text="元ファイルを表示できません")
                return
            if reset_page:
                preview_page[0] = 0
            try:
                image, page_number, page_count = self._load_preview_image(path, preview_page[0], preview_zoom[0])
            except Exception as exc:  # noqa: BLE001
                preview_canvas.delete("all")
                preview_photo[0] = None
                preview_status.configure(text=f"プレビュー表示に失敗しました: {type(exc).__name__}")
                return
            preview_page[0] = page_number
            preview_photo[0] = ImageTk.PhotoImage(image)
            preview_canvas.delete("all")
            preview_canvas.create_image(0, 0, image=preview_photo[0], anchor="nw")
            preview_canvas.configure(scrollregion=(0, 0, image.width, image.height))
            page_text = f" / {page_count} ページ" if page_count > 1 else ""
            preview_status.configure(
                text=f"{path.name}  {page_number + 1}{page_text}  ({preview_zoom[0] * 100:.0f}%)"
            )

        def shift_preview_page(offset: int) -> None:
            preview_page[0] += offset
            render_preview()

        def change_preview_zoom(multiplier: float) -> None:
            preview_zoom[0] = max(0.35, min(2.2, preview_zoom[0] * multiplier))
            render_preview()

        ttk.Button(preview_controls, text="前ページ", command=lambda: shift_preview_page(-1)).pack(side=RIGHT, padx=(5, 0))
        ttk.Button(preview_controls, text="次ページ", command=lambda: shift_preview_page(1)).pack(side=RIGHT, padx=(5, 0))
        ttk.Button(preview_controls, text="縮小", command=lambda: change_preview_zoom(0.8)).pack(side=RIGHT, padx=(5, 0))
        ttk.Button(preview_controls, text="拡大", command=lambda: change_preview_zoom(1.25)).pack(side=RIGHT, padx=(5, 0))

        def row_values(row: dict[str, Any]) -> tuple[str, ...]:
            confidence = row["record"].get("confidence")
            confidence_text = f"{float(confidence) * 100:.1f}%" if confidence is not None else "-"
            return (
                row["source_filename"],
                str(row["record"].get("vehicle_model") or ""),
                str(row["record"].get("vehicle_number") or ""),
                str(row["record"].get("pickup_datetime") or ""),
                str(row["record"].get("pickup_location") or ""),
                str(row["record"].get("delivery_datetime") or ""),
                str(row["record"].get("delivery_location") or ""),
                confidence_text,
            )

        def refresh_tree(select_index: int | None = None) -> None:
            for item_id in tree.get_children():
                tree.delete(item_id)
            for index, row in enumerate(review_rows):
                tree.insert("", END, iid=str(index), values=row_values(row))
            if review_rows:
                index = select_index if select_index is not None and select_index < len(review_rows) else 0
                tree.selection_set(str(index))
                tree.focus(str(index))
                load_selected()

        def save_entries() -> None:
            index = selected_index[0]
            if index is None or index >= len(review_rows):
                return
            record = review_rows[index]["record"]
            for field, entry in entries.items():
                record[field] = entry.get().strip() or None
            record["needs_review"] = False
            record["review_status"] = "REVIEWED"
            record["validation_json"] = {
                **(record.get("validation_json") or {}),
                "manual_confirmation_completed": True,
                "needs_review": False,
            }
            tree.item(str(index), values=row_values(review_rows[index]))

        def load_selected(_event: object | None = None) -> None:
            selection = tree.selection()
            if not selection:
                return
            new_index = int(selection[0])
            if selected_index[0] is not None and selected_index[0] != new_index:
                save_entries()
            selected_index[0] = new_index
            record = review_rows[new_index]["record"]
            for field, entry in entries.items():
                entry.delete(0, END)
                entry.insert(0, str(record.get(field) or ""))
            voucher_index = int(review_rows[new_index]["voucher_index"])
            source_path = source_paths[voucher_index] if 0 <= voucher_index < len(source_paths) else None
            if source_path != preview_path[0]:
                preview_path[0] = source_path
                preview_zoom[0] = 0.85
                render_preview(reset_page=True)

        def add_row() -> None:
            save_entries()
            review_rows.append(self._empty_review_row(vouchers, 0))
            refresh_tree(len(review_rows) - 1)

        def delete_row() -> None:
            selection = tree.selection()
            if not selection:
                return
            del review_rows[int(selection[0])]
            selected_index[0] = None
            if not review_rows:
                review_rows.append(self._empty_review_row(vouchers, 0))
            refresh_tree()

        def open_source() -> None:
            selection = tree.selection()
            if not selection:
                return
            voucher_index = int(review_rows[int(selection[0])]["voucher_index"])
            source_path = source_paths[voucher_index] if 0 <= voucher_index < len(source_paths) else None
            if source_path and source_path.exists():
                os.startfile(source_path)  # type: ignore[attr-defined]
            else:
                messagebox.showinfo("元ファイルなし", "選択した行の元ファイルを開けません。", parent=dialog)

        def complete_review() -> None:
            save_entries()
            active_rows = [
                row
                for row in review_rows
                if any(
                    row["record"].get(field)
                    for field in ("vehicle_model", "vehicle_number", "pickup_location", "delivery_location")
                )
            ]
            if not active_rows:
                messagebox.showwarning("陸送情報未入力", "確認後の陸送情報を 1 行以上入力してください。", parent=dialog)
                return
            for row in active_rows:
                record = row["record"]
                record["needs_review"] = False
                record["review_status"] = "REVIEWED"
                record["validation_json"] = {
                    **(record.get("validation_json") or {}),
                    "manual_confirmation_completed": True,
                    "needs_review": False,
                }
            self._apply_review_rows(vouchers, active_rows)
            self.validation_confirmed = True
            self._append_log(f"Validation 確認完了: {len(active_rows)}行を Excel 出力対象として保存します。")
            close_dialog()

        def cancel_review() -> None:
            self.validation_confirmed = False
            close_dialog()

        def close_dialog() -> None:
            dialog.grab_release()
            dialog.destroy()
            self.validation_dialog = None
            if self.validation_confirmation_event is not None:
                self.validation_confirmation_event.set()

        ttk.Button(list_actions, text="元ファイルを開く", command=open_source).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(list_actions, text="行を削除", command=delete_row).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(list_actions, text="行を追加", command=add_row).pack(side=RIGHT, padx=(8, 0))
        tree.bind("<<TreeviewSelect>>", load_selected)

        footer = ttk.Frame(dialog, padding=(18, 8, 18, 16))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Button(footer, text="出力を中止", command=cancel_review).pack(side=RIGHT)
        ttk.Button(footer, text="確認済みとして Excel 出力", command=complete_review).pack(side=RIGHT, padx=(0, 10))
        dialog.protocol("WM_DELETE_WINDOW", cancel_review)
        refresh_tree()

    @staticmethod
    def _flatten_review_rows(vouchers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for voucher_index, voucher in enumerate(vouchers):
            for record in voucher.get("transfer_records", []):
                rows.append(
                    {
                        "voucher_index": voucher_index,
                        "source_filename": voucher.get("source_filename") or "",
                        "record": dict(record),
                    }
                )
        return rows

    @staticmethod
    def _empty_review_row(vouchers: list[dict[str, Any]], voucher_index: int) -> dict[str, Any]:
        source_filename = vouchers[voucher_index].get("source_filename") if vouchers else ""
        return {
            "voucher_index": voucher_index,
            "source_filename": source_filename or "",
            "record": {
                "vehicle_model": None,
                "vehicle_number": None,
                "pickup_datetime": None,
                "pickup_location": None,
                "delivery_datetime": None,
                "delivery_location": None,
                "confidence": 1.0,
                "needs_review": False,
                "notes": None,
                "validation_json": {"manual_confirmation_completed": True, "needs_review": False},
            },
        }

    @staticmethod
    def _apply_review_rows(vouchers: list[dict[str, Any]], review_rows: list[dict[str, Any]]) -> None:
        for voucher in vouchers:
            voucher["transfer_records"] = []
        for row in review_rows:
            voucher_index = int(row["voucher_index"])
            if 0 <= voucher_index < len(vouchers):
                vouchers[voucher_index]["transfer_records"].append(row["record"])

    @staticmethod
    def _load_preview_image(source_path: Path, page_number: int, zoom: float) -> tuple[Image.Image, int, int]:
        zoom = max(0.35, min(2.2, float(zoom)))
        if source_path.suffix.lower() == ".pdf":
            import fitz

            with fitz.open(source_path) as document:
                if document.page_count < 1:
                    raise ValueError("PDF has no pages")
                page_number = max(0, min(int(page_number), document.page_count - 1))
                pixmap = document.load_page(page_number).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                return image, page_number, document.page_count

        with Image.open(source_path) as original:
            image = original.convert("RGB")
        width = max(1, int(image.width * zoom))
        height = max(1, int(image.height * zoom))
        return image.resize((width, height), Image.Resampling.LANCZOS), 0, 1

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
