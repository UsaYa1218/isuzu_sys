Transfer Summary Tool
=====================
正式名称: 伝票自動転記ツール

起動方法
--------
1. TransferSummaryTool.exe を実行します。
2. アプリ画面にPDFや画像をドラッグ&ドロップします。
3. 「これらのファイルを処理」を押します。
4. OCR処理後、runtime\exports にExcelが作成されます。

初回起動
--------
初回起動時に runtime フォルダを作成し、OCRエンジンとLLMモデルを確認します。
Ollama がインストールされている場合は、設定されたモデルを pull します。
GPU OCRを使うため、NVIDIA GPU、対応ドライバ、CUDA 12.6 系の実行環境が必要です。

フォルダ
--------
runtime\inbox      取込前のPDF/画像を置く場所
runtime\processed  取込済みファイルの移動先
runtime\uploads    アプリ内部の保存先
runtime\exports    Excel出力先
runtime\app.db     処理履歴のSQLiteデータベース

LLM設定
-------
同じフォルダに .env を置くと設定を変更できます。
Ollamaをローカルで使う場合は、Ollamaを起動してから本アプリを起動してください。

主な設定例:
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:14b
OLLAMA_API_STYLE=ollama
OLLAMA_THINK=false
PADDLEOCR_USE_GPU=true

終了方法
--------
アプリ画面を閉じると終了します。
