# solution3 作業まとめ

更新日: 2026-04-17

## 概要

`deep-research-report.md` をもとに、ローカル完結で動作する伝票自動転記ツールの MVP を構築した。  
現在の構成は `FastAPI + SQLite + PaddleOCR + Ollama` で、PDF/画像のアップロード、OCR、伝票情報の抽出、レビュー、エクスポートまで一通り動作する。

## 採用技術

- Web アプリ: `FastAPI`
- OCR: `PaddleOCR`
- PDF 処理: `PyMuPDF`
- LLM 正規化: `Ollama`
- データ保存: `SQLite`
- テンプレート/UI: `Jinja2`
- エクスポート: `openpyxl`, `csv`, `zip`

## 現在の主要機能

- PDF / 画像ファイルのアップロード
- `PaddleOCR` による OCR 実行
- OCR テキストからの伝票項目抽出
- `Ollama` による JSON 正規化
- 抽出結果のレビュー画面表示
- Excel / CSV(zip) エクスポート
- 監査ログ保存

## LLM / OCR 設定

- LLM: `Ollama`
- 現在のモデル設定: `qwen2.5:7b`
- OCR: `PaddleOCR`
- 利用中の OCR モデル:
  - `PP-OCRv5_server_det`
  - `PP-OCRv5_server_rec`

## 環境構築まわり

PowerShell の実行ポリシーで `.ps1` がそのまま動かない環境向けに、Windows で実行しやすい `setup.cmd` と `run.cmd` を用意した。

対応済み事項:

- `.venv` 仮想環境の作成
- `requirements.txt` の依存関係インストール
- `paddlepaddle==3.2.0` の CPU 版インストール
- `Ollama` とモデルの初期取得
- `runtime/uploads`, `runtime/exports`, `runtime/tmp` などの生成
- `.env.example` から `.env` を作成する流れの整備

補足:

- `setup.ps1` は過去に文字化けと構文崩れがあったため、ASCII ベースで組み直して修正済み
- PowerShell の実行制限回避のため、通常は `cmd /c setup.cmd` / `cmd /c run.cmd` を使う想定

## OCR パイプラインの改善

`app/services/ocr_pipeline.py` を中心に OCR 周辺を調整した。

実施内容:

- PaddleOCR 3 系の `predict()` に対応
- 旧 API 向け `ocr()` フォールバックを維持
- PDF を画像化して OCR する処理を整理
- 一時ディレクトリやキャッシュ出力先を `runtime/` 配下に寄せた
- `preprocess_image()` を調整し、OCR 前処理後も RGB で扱うように修正
- synthetic PNG で OCR が通ることを確認

確認済み:

- OCR テキストとして `Invoice No INV-001`, `Date: 2026/04/15`, `Total:11000` を取得できることを確認済み

## 詳細画面の不具合修正

レビュー画面 `/vouchers/{voucher_id}` で 500 が出ていた問題を修正した。

原因:

- Jinja テンプレート側で `voucher.items` が Python の組み込みメソッド解釈になり、反復で失敗していた
- テンプレートに文字化けがあり、保守しづらい状態だった

対応:

- `app/templates/voucher_detail.html` を整理して再構成
- `TemplateResponse` 呼び出しを現行の Starlette/FastAPI 仕様に合わせて見直し
- 詳細画面が `200` で返ることを確認

## PDF の表抽出対応

当初は PDF 内の表が OCR テキストの並びとしてしか扱われず、列や行の意味が失われていた。  
これに対し、表を表構造として保持する処理を追加した。

### 実装内容

- `app/schemas.py`
  - `ExtractedTable` を追加
  - `ExtractionResult.tables` を追加
- `app/services/ocr_pipeline.py`
  - `extract_tables()` を追加
  - まず `PyMuPDF` の `find_tables()` を試す
  - 取れない場合は、ページ画像上の罫線検出と OCR 座標から表グリッドを再構成するフォールバックを実装
- `app/main.py`
  - OCR 実行後に `extract_tables(source_path, ocr_lines=lines)` を呼ぶように変更
  - `document_json` と `raw_ocr_json` に表情報を保存
- `app/database.py`
  - `document_json.tables` の既定値を補う処理を追加
- `app/templates/voucher_detail.html`
  - `Detected Tables` セクションを追加
- `app/static/styles.css`
  - 表表示用のスタイルを追加

### 実データでの確認結果

対象伝票 `v_86e18e7646f4` に対して確認済み。

結果:

- `find_tables()` は 0 件
- ただし PDF 画像上には罫線つき表が存在
- OCR 座標 + 罫線グリッド再構成で表を 1 件抽出
- 復元結果:
  - ヘッダー 11 列
  - データ 2 行

保存確認:

- `document_json.tables` に 1 件入ることを確認
- 詳細画面 `/vouchers/v_86e18e7646f4` で表が表示されることを確認

## エクスポート改善

レビュー画面で見えている表を、エクスポートにも含めるように変更した。

### Excel 出力

`export_voucher_xlsx()` を拡張し、次のシート構成にした。

- `header`
- `items`
- `tables_index`
- `table_p{page}_{index}`

`tables_index` には以下を出力する。

- `voucher_id`
- `page`
- `table_index`
- `sheet_name`
- `column_count`
- `row_count`
- `bbox_json`

### CSV(zip) 出力

`export_voucher_csv_zip()` を拡張し、次のファイルを含めるようにした。

- `voucher_header.csv`
- `voucher_items.csv`
- `voucher_tables_index.csv`
- `voucher_table_p{page}_{index}.csv`

### 実出力確認

対象伝票 `v_86e18e7646f4` で確認済み。

確認内容:

- Excel に `table_p1_1` シートが追加される
- ZIP に `voucher_table_p1_1.csv` が含まれる
- 表データが 11 列 2 行で出力される

## ログについての整理

PaddleOCR / Paddle 実行時にいくつかログが出ることを確認している。

主な内容:

- `lang and ocr_version will be ignored ...`
  - ローカルモデルディレクトリを明示指定しているための注意
- `Creating model: ('PP-OCRv5_server_det', ...)`
  - モデルロード開始の情報
- `No ccache found`
  - C/C++ 拡張の再コンパイル高速化ツールがないという注意
- `oneDNN v3.6.2`
  - CPU ライブラリ初期化ログ

現状判断:

- いずれも OCR 失敗を直接意味するものではない
- 実害は小さく、主に初期化時の情報ログ

## Colab / 高性能モデルの検討

`solution3` 全体を Colab 拡張でそのまま運用するのは適していないが、試験運用として高性能な LLM を比較する用途には有効と整理した。

理由:

- 本番運用予定ではない
- 現在の PC より高性能な環境でモデル比較したい
- `solution3` は `OLLAMA_BASE_URL` を切り替えられるため、LLM 部分だけ外出ししやすい

## Colab 前提での候補モデル整理

`qwen2.5:7b` より上の候補として、`solution3` 向けに以下を有力候補として整理した。

優先候補:

- `qwen2.5:14b`
- `qwen3:14b`
- `qwen3:30b`
- `qwen2.5:32b`

補欠候補:

- `gemma3:12b`
- `gemma3:27b`

判断基準:

- 日本語対応
- OCR 後テキストからの JSON 正規化の安定性
- 表や構造化データ理解
- 指示追従の素直さ

現時点の推奨比較順:

1. `qwen2.5:14b`
2. `qwen3:14b`
3. `qwen3:30b`
4. `qwen2.5:32b`

## 現在の確認済み状態

- 環境構築は一通り完了
- `cmd /c run.cmd` でアプリ起動可能
- 既存伝票の詳細画面表示は正常
- 表抽出結果は画面に表示可能
- 表を含む Excel / CSV(zip) エクスポートが可能

## 既知の課題

- OCR 由来の誤認識は残る
- 表セル内で上下 2 段に分かれた文字が、1 セル内改行として入ることがある
- 伝票種別ごとの抽出ロジック `app/services/extraction.py` は文字化けを含む箇所があり、将来的に整理が必要
- `qwen3` を使う場合は thinking 挙動が JSON 出力の邪魔になる可能性があり、`no_think` 的な制御を検討余地あり

## 次の候補作業

- モデル比較をしやすいように複数モデル一括評価機能を追加
- `llm.py` にモデル別オプションを入れて `qwen3` を non-thinking 寄りで使えるようにする
- `extraction.py` の文字化け解消と抽出ロジックの保守性改善
- 表セルの後補正ルールを追加し、列ごとの OCR 誤認識を減らす
