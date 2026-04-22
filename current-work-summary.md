# solution3 作業まとめ

更新日: 2026-04-17

## 概要

`solution3` は `FastAPI + SQLite + PaddleOCR + Ollama` 構成の伝票自動転記 MVP。  
ローカルの Web アプリとして動作し、PDF / 画像アップロード、OCR、項目抽出、レビュー、Excel / CSV(zip) 出力まで一通り実装済み。

今日は主に以下を進めた。

- LPR88 系 PDF の表抽出改善
- レビュー画面の日本語化と視認性改善
- 複数ファイルアップロード対応
- Colab Computing Unit を使う Ollama 連携
- Colab Computing Unit を使う OCR オフロードの基盤追加
- LLM 実行状態の可視化

## 現在の主な機能

- PDF / 画像ファイルのアップロード
- 単票または複数ファイルの OCR 処理
- PaddleOCR による OCR
- ルールベース + Ollama による項目抽出補助
- 表抽出とレビュー画面での表示
- 元 PDF / 画像プレビュー付きレビュー画面
- Excel / CSV(zip) 出力
- 監査ログ保存

## 今日の実装内容

### 1. レビュー保存と状態再計算

- レビュー画面で編集した内容から `document_json` / `validation_json` / `status` / `needs_review` を再計算するように修正
- 保存後に画面表示と DB 状態がずれないようにした

対象:

- `app/main.py`

### 2. Colab Ollama 連携

- `#%%` ベースの Colab / Interactive Window 向け Ollama 起動スクリプトを追加
- `OLLAMA_BASE_URL` を外部 URL に切り替えるだけで、ローカルアプリから Colab 上の Ollama を利用できるようにした
- 既定モデルを `qwen3:14b` に更新
- 追加ヘッダーや generate options を `.env` から渡せるようにした

対象:

- `colab/start_ollama_colab.py`
- `app/config.py`
- `app/services/llm.py`
- `.env.example`
- `docs/colab-ollama.md`
- `README.md`

### 3. OCR の改善

- OCR 前処理を強化
  - autocontrast
  - 小さめ画像の拡大
  - median filter
  - unsharp mask
- OCR 後の日本語補正辞書を追加
  - `いすぶ -> いすゞ`
  - `输送 -> 輸送`
  - `ライネツクス -> ライネックス`
  - `營業時間 -> 営業時間`
  - ほか業務用語の補正
- PaddleOCR ローカルモデルが `runtime/paddleocr` に無い場合でも、ユーザーキャッシュへフォールバックするように変更
- `PADDLEOCR_MAX_SIDE_LIMIT` を導入し、PaddleOCR 側の自動縮小を制御

対象:

- `app/services/ocr_pipeline.py`
- `app/config.py`
- `.env.example`

### 4. 特殊帳票の表抽出改善

#### XZC605W 系

- 上段の輸送区間を `項目 / 発地 / 経由地 / 着地` の表として再構成
- 矢印部分で分割せず、1 つの表として扱うようにした

#### JB1PROF 系

- 搬送依頼票を
  - `項目 / 出発地 / 搬送先`
  - `項目 / 引取場所 / 納入場所`
  の 2 表へ再構成

#### LPR88 系

- 巨大な 1 表として潰れていたレイアウトを
  - `依頼情報`
  - `発着地情報`
  - `車両一覧`
  の 3 表へ分割
- `RN` が `POSNo.` に入ってしまうケースを補正し、`車輌名称` 側へ戻す処理を追加
- 元表で空欄だったセルを LLM 再構成後も空欄のまま維持する補正を追加

対象:

- `app/services/ocr_pipeline.py`
- `app/services/extraction.py`

### 5. 明細抽出の改善

- OCR 行から明細が取れない場合、抽出済み表から `items` を起こすようにした
- `MODEL / Vin / 現在地 / 全長 / 全幅 / 全高 / 重量` のような車両スペック表にも対応
- `車台番号`、`車輌名称`、`オーダーNo.` などのヘッダを明細抽出の候補に追加

対象:

- `app/services/extraction.py`

### 6. LLM 補正の改善

- 日付比較を正規化して OCR 文面と照合するようにした
  - `2022/5/19` と `2022-05-19` を同一視
- `JPY` は OCR に他通貨明示がない場合は許容
- 通常の項目正規化に加え、低品質な表だけを対象にした「表再構成」プロンプトを追加
- LLM の実行状態を `unused / failed / applied` で保持するようにした
- レビュー画面に `LLM詳細` を表示し、接続失敗や JSON 解析失敗の内容が分かるようにした

対象:

- `app/services/llm.py`
- `app/services/extraction.py`
- `app/templates/voucher_detail.html`
- `app/schemas.py`
- `app/database.py`

### 7. UI / レビュー画面の改善

- 画面全体を日本語寄りに整理
- 状態、種別、レビュー項目、セクション名を日本語で表示
- 詳細画面に元 PDF / 画像プレビューを追加
- 抽出表にタイトルと行数を表示
- トップ画面に現在の LLM モデル名を表示

対象:

- `app/templates/index.html`
- `app/templates/voucher_detail.html`
- `app/static/styles.css`
- `app/main.py`

### 8. 複数ファイルアップロード対応

- アップロードフォームを `multiple` に変更
- `/upload` を `list[UploadFile]` 受け取りに変更
- 複数選択時は一覧へ戻し、それぞれ OCR バックグラウンド処理へ投入

対象:

- `app/templates/index.html`
- `app/main.py`

### 9. Colab OCR オフロード基盤

- ローカル PC が重くなる問題に対応するため、OCR を Colab Computing Unit 側へ逃がす仕組みを追加
- ローカルアプリ側に `REMOTE_OCR_BASE_URL` を追加
- 設定されている場合はローカル OCR ではなく外部 OCR ワーカーへ送信
- Colab 側に `#%%` ベースの OCR ワーカー起動スクリプトを追加

現状:

- ローカル側の分岐は実装済み
- Colab 側 OCR ワーカーはスレッド起動版へ置き換え済み
- 実運用確認は継続中

対象:

- `app/config.py`
- `app/services/ocr_pipeline.py`
- `colab/start_ocr_colab.py`
- `.env.example`

## 現在の設定

### LLM

- `OLLAMA_MODEL=qwen3:14b`
- `OLLAMA_BASE_URL` は Colab の `trycloudflare` URL を使用

### OCR

- `PADDLEOCR_LANG=japan`
- `PADDLEOCR_MAX_SIDE_LIMIT=5600`
- `REMOTE_OCR_BASE_URL` は未設定時ローカル OCR、設定時は Colab OCR を使用

## 確認済み事項

- `.venv` 上で主要 Python ファイルの `py_compile` は通過
- LPR88 系で表が 3 分割されることを保存済み OCR データで確認
- 複数アップロードのルートはコード上実装済み
- LLM 状態表示は `利用済み / 実行失敗 / 未使用` に分岐済み
- Colab Ollama 用スクリプトは `qwen3:14b` 前提に更新済み

## 既知の課題

- OCR 由来の誤認識はまだ残る
- 帳票によってはベンダー名や金額抽出が弱い
- Colab の `trycloudflare` URL は毎回変わるため、`.env` 更新が必要
- Colab OCR ワーカーは実運用の最終確認がまだ必要
- `current-work-summary.md` 以外のドキュメント更新は一部追随余地あり

## 次の候補作業

- Colab OCR ワーカーの接続確認を完了し、`REMOTE_OCR_BASE_URL` まで通す
- LPR88 以外の帳票でも列補正ルールを一般化する
- ベンダー名 / 取引先名の抽出精度を改善する
- LLM を使った表再構成の適用条件をさらに安定化する
- 複数モデル比較や一括評価用の画面 / スクリプトを追加する
