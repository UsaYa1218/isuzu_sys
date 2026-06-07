# solution3 作業まとめ

更新日: 2026-05-08

## 概要

`solution3` は、FastAPI / SQLite / PaddleOCR / LLM を使った伝票OCR・転記支援アプリです。
PDF / 画像をアップロードし、OCR、項目抽出、LLM補正、レビュー、Excel / CSV 出力までを扱います。

今回の更新では、Colab 上の `gpt-oss-120b` を OpenAI 互換 API として利用するための設定と、OCR / LLM 抽出の安定化を進めました。

## 今回の主な更新

### 1. gpt-oss-120b Colab 連携

- `gpt-oss-120b` を Colab A100 で動かすための notebook / 起動設定を追加。
- OpenAI 互換エンドポイントを既存の LLM 設定から使えるようにした。
- `profiles.gpt-oss-120b.json` に Colab 用プロファイルを追加。
- `.env.gpt-oss-120b.example` を追加し、接続設定を分けて管理できるようにした。

対象:

- `colab/gpt_oss_120b_colab_a100.ipynb`
- `profiles.gpt-oss-120b.json`
- `.env.gpt-oss-120b.example`
- `app/config.py`
- `app/services/llm.py`

### 2. LLM 実行方式の拡張

- Ollama API に加えて OpenAI Responses API / Chat Completions 互換 API に対応。
- `OLLAMA_API_STYLE`、`OLLAMA_API_KEY`、`OLLAMA_THINK` を追加。
- JSON schema を使った構造化レスポンス指定を追加。
- Markdown コードフェンスや余分な前後テキストを含む LLM 応答から JSON を取り出す処理を追加。
- LLM の正規化結果に `context_hints` を追加し、会社名、人物名、住所、電話番号、メモなどの補助情報を残せるようにした。

対象:

- `app/config.py`
- `app/services/llm.py`
- `app/schemas.py`
- `app/database.py`
- `app/templates/voucher_detail.html`

### 3. OCR 処理の改善

- PaddleOCR の GPU 優先実行と CPU フォールバックを分離。
- OCR 実行ログを標準出力へ出し、ページ単位の進捗を追いやすくした。
- リモート OCR 失敗時にローカル OCR へフォールバックするログを追加。
- PaddleOCR モデル探索先にユーザーキャッシュを追加。
- OCR テーブルセルを NFKC 正規化し、全角・半角差による抽出揺れを抑制。

対象:

- `app/services/ocr_pipeline.py`
- `app/config.py`
- `.env.example`

### 4. 搬送依頼系テーブルの再構成

- 複数行に分かれる搬送依頼明細を、レビューしやすい表に再構成する処理を追加。
- `車型`、`車番`、`引取可能日時`、`所在場所`、`搬入希望日時`、`搬入場所`、`負担部署`、`支払い担当`、`保険`、`搬送目的` などを整理。
- コンパクトな1件形式の搬送依頼表も抽出できるようにした。

対象:

- `app/services/ocr_pipeline.py`
- `app/services/extraction.py`

### 5. 起動時・処理状態の扱い

- `REQUEUE_PROCESSING_OCR_ON_STARTUP` を追加。
- 必要に応じて、起動時に処理中で残った OCR ジョブを再投入できるようにした。

対象:

- `app/config.py`
- `app/main.py`
- `.env.example`

### 6. LLM 比較・検証スクリプト

- テストデータに対して複数 LLM プロファイルを比較するスクリプトを追加。
- 実行結果は `runtime/llm_compare/` 配下に保存する想定。
- `runtime/` は `.gitignore` 対象のため、比較結果 JSON や OCR キャッシュは Git 管理外。

対象:

- `scripts/compare_llm_on_testdata.py`
- `scripts/bootstrap_expected_fields.py`

## 現在の設定メモ

### LLM

- Colab 連携時は `OLLAMA_BASE_URL` に Cloudflare Tunnel の URL を設定。
- `OLLAMA_MODEL=openai/gpt-oss-120b`
- `OLLAMA_API_STYLE=openai`
- `OLLAMA_API_KEY=EMPTY`

### OCR

- `REMOTE_OCR_BASE_URL` が設定されていればリモート OCR を優先。
- リモート OCR 失敗時はローカル PaddleOCR にフォールバック。
- `PADDLEOCR_USE_GPU=true` の場合も、GPU 実行失敗時は CPU にフォールバック。

## Git 管理対象外

以下は実行時生成物として Git には含めない。

- `runtime/`
- `.env`
- `__pycache__/`
- `*.pyc`

## 次の確認候補

- `gpt-oss-120b-colab` プロファイルで複数帳票を再評価し、項目抽出精度を確認する。
- 搬送依頼以外の帳票でも、LLM の `context_hints` がレビュー画面で有効に見えるか確認する。
- `REQUEUE_PROCESSING_OCR_ON_STARTUP` を有効にした場合の再投入挙動を実データで確認する。
