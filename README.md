# 伝票自動転記ツール

PDF/画像の伝票をOCRとLLMで読み取り、陸送一覧Excelを作成するWindows向けツールです。

最終的なExcelには以下の8項目だけを出力します。

- 車型・車番
- 搬出日時
- 搬出場所
- 搬入日時
- 搬入場所
- 実行者
- 実行日時
- 読み取りファイル名

## デスクトップアプリ

配布用アプリはブラウザを使わず、通常のWindowsアプリとして起動します。

起動ファイル:

```text
dist\TransferSummaryTool\TransferSummaryTool.exe
```

使い方:

1. `TransferSummaryTool.exe` を起動します。
2. PDFまたは画像ファイルをアプリ画面へドラッグ&ドロップします。
3. `これらのファイルを処理` を押します。
4. 処理完了後、Excelが `runtime\exports` に作成されます。

画面にはOCRの進捗が表示されます。

```text
example.pdf OCR中 80% (2/3ページ完了)
```

画面下部の進捗バーは全体進捗を示し、近くに残り時間の目安を表示します。

```text
残り時間: 約2分30秒
```

## 初回起動

初回起動時に以下を確認・準備します。

- `runtime` 配下の作業フォルダ作成
- GPU OCR環境の確認
- Ollamaがインストールされている場合、設定されたLLMモデルの確認・pull

GPU OCRは配布先PCの環境に合わせて判定します。

- `nvidia-smi` でNVIDIA GPUとCUDAバージョンを確認
- CUDA 12.6以上: `cu126` 用の `paddlepaddle-gpu==3.2.0` を導入
- CUDA 11.8以上: `cu118` 用の `paddlepaddle-gpu==3.2.0` を導入
- 対応GPU/CUDAが見つからない場合はCPU OCRへフォールバック

GPU版ライブラリは以下へ追加導入され、アプリ起動時に優先利用されます。

```text
runtime\python_packages
```

初回GPUライブラリ導入には、配布先PCにインターネット接続とPythonが必要です。

## 設定

アプリと同じフォルダの `.env` で設定できます。

主な項目:

```env
APP_NAME=伝票自動転記ツール
PADDLEOCR_USE_GPU=true
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:14b
OLLAMA_API_STYLE=ollama
OLLAMA_THINK=false
```

`PADDLEOCR_USE_GPU=true` が既定です。GPU環境が使えない場合は自動でCPUに切り替わります。

## フォルダ

```text
runtime\inbox            一括取込用のPDF/画像置き場
runtime\processed        inbox取込済みファイルの移動先
runtime\uploads          アプリ内部の保存先
runtime\exports          Excel出力先
runtime\python_packages  初回起動時に追加導入するGPUライブラリ
runtime\app.db           処理履歴のSQLiteデータベース
```

## 開発環境セットアップ

```cmd
cd solution3
setup.cmd
```

CPU版PaddlePaddleでセットアップしたい場合:

```cmd
setup.cmd -UseCpuPaddle
```

PowerShellで実行する場合:

```powershell
cd solution3
.\setup.ps1
```

## Web版の起動

開発用のWeb UIも残しています。

```cmd
run.cmd
```

起動後、ブラウザで以下を開きます。

```text
http://127.0.0.1:8000
```

## 配布パッケージ作成

GUI版の配布パッケージを作る場合:

```cmd
build_windows_package.cmd
```

出力先:

```text
dist\TransferSummaryTool
```

配布時は `dist\TransferSummaryTool` フォルダごと渡してください。

## テストデータでExcel生成

OCRキャッシュ済みのテストデータからExcelを生成できます。

```cmd
.\.venv\Scripts\python.exe scripts\export_transfer_summary_from_testdata.py
```

出力先:

```text
runtime\exports
```

## 注意

- LLM抽出にはOllamaまたはOpenAI互換APIが必要です。
- Ollamaをローカルで使う場合は、アプリ起動前にOllamaを起動してください。
- 初回GPUライブラリ導入は時間がかかる場合があります。
- GPU OCRにはNVIDIA GPU、対応ドライバ、対応CUDA環境が必要です。
- GPU OCRが失敗した場合でも、処理はCPU OCRへフォールバックします。
