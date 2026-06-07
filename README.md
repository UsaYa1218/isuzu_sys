# 伝票自動転記ツール (`solution3`)

PDF または画像の伝票を OCR と LLM で読み取り、陸送情報を Excel に出力する Windows 向けアプリケーションです。

現行版には、簡易操作用のデスクトップ GUI と、確認・修正を行える Web UI の 2 種類があります。

## 出力内容

陸送一覧 Excel は次の 8 項目を出力します。

- 車型・車番
- 搬出日時
- 搬出場所
- 搬入日時
- 搬入場所
- 実行者
- 実行日時
- 読み取りファイル名

入力として使用できる形式は `.pdf`、`.png`、`.jpg`、`.jpeg`、`.webp` です。

## 利用方法の選択

### デスクトップ GUI

複数ファイルを追加し、陸送一覧 Excel を作成する用途向けです。Validation が基準を下回った場合は、Excel 作成前に元 PDF/画像のプレビュー付き確認・修正画面を表示します。

配布済み ZIP:

```text
packages\TransferSummaryTool.zip
```

起動手順:

1. ZIP を任意のフォルダに展開します。
2. 必要に応じて展開先の `.env` を設定します。
3. `TransferSummaryTool.exe` を起動します。
4. PDF/画像をドラッグ&ドロップ、または `ファイル追加` で選択します。
5. `これらのファイルを処理` を押します。
6. 作成された Excel を `runtime\exports` で確認します。

デスクトップ GUI では、OCR ページ進捗、全体進捗、残り時間の目安が画面に表示されます。

### ブラウザ UI の配布版

ファイル登録、OCR 結果の確認・修正、状態管理、個別 Excel/CSV、複数伝票の陸送一覧 Excel 出力を使用する場合はこちらを利用します。

配布済み ZIP:

```text
packages\TransferSummaryBrowserTool.zip
```

起動手順:

1. ZIP を任意のフォルダに展開します。
2. 必要に応じて展開先の `.env` を設定します。
3. `TransferSummaryBrowserTool.exe` を起動します。
4. 自動的に開くブラウザ画面から PDF/画像を取り込みます。

### ソースからの Web UI 起動

開発・動作確認・設定調整を行う場合の手順です。起動後は `http://127.0.0.1:8000` を開きます。

```powershell
git clone https://github.com/UsaYa1218/isuzu_sys.git
cd isuzu_sys
.\setup.ps1
.\run.ps1
```

Command Prompt を使用する場合:

```cmd
git clone https://github.com/UsaYa1218/isuzu_sys.git
cd isuzu_sys
setup.cmd
run.cmd
```

`setup.ps1` / `setup.cmd` は次を実行します。

- Python 仮想環境 `.venv` の作成
- `requirements.txt` のインストール
- `.env` がない場合の `.env.example` からの作成
- OCR 用 PaddlePaddle のインストール
- `runtime` 配下の作業フォルダ作成
- Ollama が使用可能な場合のモデル取得

CPU のみで OCR 環境を構成する場合:

```powershell
.\setup.ps1 -UseCpuPaddle
```

Ollama に取得させるモデルを指定する場合:

```powershell
.\setup.ps1 -OllamaModel "qwen3:14b"
```

## Web UI の機能

- PDF/画像の直接アップロード、または `runtime\inbox` からの一括取込
- 請求書、納品・陸送系、仕訳伝票の種別選択
- OCR および LLM 抽出結果の詳細表示と手動修正
- 処理後の Validation 信頼度表示と、低信頼度時の確認必須化
- 状態管理 (`確認要`、`承認待ち`、`承認済み` など)
- 伝票単位の Excel / CSV (ZIP) 出力
- 一括処理単位または全件の陸送一覧 Excel 出力
- SQLite による処理履歴保存

Validation の最低信頼度が確認基準を下回る場合、または確認メッセージがある場合は、元伝票との照合確認を保存するまで承認操作および Excel/CSV 出力は実行できません。デスクトップ GUI では、該当する結果は Excel 作成前に元 PDF/画像を画面内でプレビューできる確認・修正画面を表示し、確認を完了するまで出力しません。

## 設定ファイル

設定は実行ファイルまたはリポジトリ直下の `.env` で変更します。`.env` は Git 管理対象外です。

作業フォルダを変更する場合も、特定の PC の絶対パスは記載せず、プロジェクトを基準にした相対パスを使用してください。

```env
APP_ENV=local
APP_NAME=伝票自動転記ツール
DATA_DIR=runtime
DATABASE_PATH=runtime/app.db
UPLOAD_DIR=runtime/uploads
EXPORT_DIR=runtime/exports
INBOX_DIR=runtime/inbox
PROCESSED_DIR=runtime/processed
OCR_DPI=300
OCR_CONFIDENCE_THRESHOLD=0.75
PADDLEOCR_MAX_SIDE_LIMIT=4000
PADDLEOCR_LANG=japan
PADDLEOCR_USE_GPU=true
REMOTE_OCR_BASE_URL=
REMOTE_OCR_TIMEOUT_SECONDS=300
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:14b
OLLAMA_API_STYLE=ollama
OLLAMA_API_KEY=EMPTY
OLLAMA_THINK=false
OLLAMA_TIMEOUT_SECONDS=180
OLLAMA_HEADERS_JSON=
OLLAMA_GENERATE_OPTIONS_JSON=
REQUEUE_PROCESSING_OCR_ON_STARTUP=false
```

公開トンネル URL を利用する場合、その URL は通常一時的です。既存の設定例に残っている URL をそのまま前提にせず、起動中の OCR/LLM サービスが表示した URL に置き換えてください。

## OCR 構成

### ローカル OCR

`REMOTE_OCR_BASE_URL` が空の場合、PaddleOCR をローカルで実行します。

- `PADDLEOCR_USE_GPU=true`: GPU を優先し、実行できない場合は CPU に切り替えます。
- `PADDLEOCR_USE_GPU=false`: CPU で実行します。

デスクトップ配布版は初回起動時に NVIDIA GPU を検出すると、GPU 用 PaddlePaddle を `runtime\python_packages` に追加インストールします。この自動インストールを使用するには、インターネット接続と Python が必要です。

### リモート OCR

Colab 等で起動した OCR サービスを使用する場合は、`.env` に公開 URL を設定します。

```env
REMOTE_OCR_BASE_URL=https://YOUR-OCR-TUNNEL-URL.trycloudflare.com
REMOTE_OCR_TIMEOUT_SECONDS=300
```

リモート OCR が失敗した場合、アプリはローカル OCR を試行します。Colab 用の起動コードは `colab\start_ocr_colab.py` と `colab\start_ocr_colab.ipynb` にあります。

## LLM 構成

OCR 結果から陸送行を整形するには、Ollama または OpenAI 互換 API が必要です。LLM が利用できない場合でも、帳票テーブルから抽出できるデータについてはルールベース処理を試行します。

### ローカル Ollama

Ollama をインストール後、モデルを取得します。

```powershell
ollama pull qwen3:14b
```

`.env` の設定:

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:14b
OLLAMA_API_STYLE=ollama
OLLAMA_THINK=false
```

### Colab 上の Ollama

LLM 推論のみを Colab に移す場合は、`docs\colab-ollama.md` と `colab\start_ollama_colab.py` を参照してください。起動結果として表示される URL とモデル名を `.env` に設定します。

```env
OLLAMA_BASE_URL=https://YOUR-OLLAMA-TUNNEL-URL.trycloudflare.com
OLLAMA_MODEL=qwen3:14b
OLLAMA_API_STYLE=ollama
```

### OpenAI 互換 API

OpenAI Responses API または Chat Completions 互換のサーバーにも接続できます。Colab の `gpt-oss-120b` 用設定例は `.env.gpt-oss-120b.example` と `colab\gpt_oss_120b_colab_a100.ipynb` にあります。

```env
OLLAMA_BASE_URL=https://YOUR-OPENAI-COMPATIBLE-ENDPOINT
OLLAMA_MODEL=openai/gpt-oss-120b
OLLAMA_API_STYLE=openai
OLLAMA_API_KEY=EMPTY
OLLAMA_TIMEOUT_SECONDS=900
```

`OLLAMA_API_STYLE` は `ollama`、`openai`、`openai-responses`、`openai-chat`、または自動判定用の `auto` を指定できます。

## 必要な外部ツール

### 配布済みデスクトップ GUI を CPU で使う場合

ZIP 展開とアプリ起動だけで利用できます。ローカル LLM を使う場合は Ollama を追加でインストールしてください。

### ソースから起動する場合

以下が必要です。

| ツール | 用途 | インストール方法 |
| --- | --- | --- |
| Git | リポジトリの取得 | [Git for Windows](https://git-scm.com/download/win) からインストーラーを取得 |
| Python 3.12 | Web UI 起動、依存パッケージ導入、GPU 配布版の追加セットアップ | [Python for Windows](https://www.python.org/downloads/windows/) から 64-bit installer を取得し、インストール時に PATH 追加を有効化 |
| Ollama | ローカル LLM 推論 | [Ollama for Windows](https://ollama.com/download/windows) からインストーラーを取得、または下記の `winget` コマンドを実行 |

Windows Package Manager (`winget`) が利用可能な場合、`setup.ps1` は Python と Ollama が未導入ならインストールを試行します。手動で先に導入する場合:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Ollama.Ollama -e
```

`winget` 自体がない場合は、Microsoft の [WinGet ドキュメント](https://learn.microsoft.com/windows/package-manager/winget/) に従い App Installer を導入してください。

### GPU OCR を使用する場合

GPU OCR は必須ではありません。使用する場合は NVIDIA GPU と対応ドライバが必要です。

1. [NVIDIA Driver Downloads](https://www.nvidia.com/Download/index.aspx/) から GPU に合ったドライバを導入します。
2. PowerShell または Command Prompt で `nvidia-smi` が実行できることを確認します。
3. `PADDLEOCR_USE_GPU=true` の状態でアプリを起動します。

アプリは検出された CUDA 互換バージョンに応じて PaddlePaddle GPU 版を導入し、利用できなければ CPU OCR に切り替えます。

### Colab 連携を使用する場合

Google Colab の GPU ランタイムが必要です。Colab スクリプトは実行時に必要な Python パッケージと Cloudflare Tunnel 用 `cloudflared` をランタイムへ取得します。ローカル PC に `cloudflared` をインストールする必要はありません。

## 作業フォルダ

以下はいずれもアプリ配置フォルダまたはリポジトリ直下からの相対パスです。

```text
runtime\inbox            Web UI の一括取込に使う PDF/画像配置先
runtime\processed        inbox から取り込んだファイルの移動先
runtime\uploads          Web UI に登録された元ファイルの保存先
runtime\exports          Excel / CSV (ZIP) の出力先
runtime\paddleocr        OCR モデル保存先
runtime\python_packages  配布版が追加導入する GPU ライブラリ保存先
runtime\app.db           Web UI の処理履歴 SQLite データベース
```

## デスクトップ GUI のビルド

ソースからデスクトップ GUI の配布フォルダを生成する場合:

```cmd
build_windows_package.cmd
```

生成先:

```text
dist\TransferSummaryTool
```

配布する場合は `dist\TransferSummaryTool` フォルダ全体を渡してください。設定を配布物に含める場合は、実在する一時トンネル URL や API キーを含めないよう `.env` の内容を事前に確認してください。

## テストデータでの Excel 生成

OCR キャッシュ済みのテストデータが `runtime\testdata` と `runtime\ocr_cache` に存在する場合、次のコマンドで陸送一覧 Excel を生成できます。

```powershell
.\.venv\Scripts\python.exe .\scripts\export_transfer_summary_from_testdata.py
```

出力先は `runtime\exports` です。

## 注意事項

- `.env`、`runtime`、OCR キャッシュ、出力ファイルは Git にコミットしないでください。
- Cloudflare Quick Tunnel の URL はランタイム再起動等で変更されます。
- OCR/LLM の結果は原本と照合して確認してください。Web UI では修正と承認操作を行えます。
