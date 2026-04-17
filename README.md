# 伝票自動転記ツール

`deep-research-report.md` を基にした、ローカル完結の伝票自動転記 MVP です。  
`PaddleOCR` で OCR、`Ollama` で正規化補助を行い、レビュー後に `Excel` または `CSV(zip)` へ出力できます。

## 構成

- バックエンド: `FastAPI`
- OCR: `PaddleOCR`
- PDF レンダリング: `PyMuPDF`
- LLM: `Ollama`
- 永続化: `SQLite`
- UI: `Jinja2` テンプレート

## できること

- PDF / 画像をアップロードして OCR 実行
- 伝票種別ごとに基本フィールドを抽出
- LLM による数値 / 日付 / 表記ゆれの正規化補助
- Validation による要確認判定
- ブラウザ上でレビュー編集
- `xlsx` と `csv(zip)` の出力
- 最低限の監査ログ保持

## セットアップ

Windows PowerShell を前提にしています。

```powershell
cd solution3
.\setup.ps1
```

`setup.ps1` が行うこと:

1. `winget` が使える場合に Python / Ollama を導入
2. `.venv` 作成
3. 依存ライブラリ導入
4. 公式の CPU wheel から `paddlepaddle==3.2.0` を導入
5. `.env` 作成
6. `runtime/` 初期化
7. 既定モデル `qwen2.5:7b` を `ollama pull`

既に Python / Ollama を導入済みなら、インストーラ部分を飛ばせます。

```powershell
.\setup.ps1 -SkipInstaller
```

## 起動

```powershell
cd solution3
.\run.ps1
```

起動後、ブラウザで `http://127.0.0.1:8000` を開いてください。

PowerShell の実行ポリシーで `.ps1` が止まる環境では、`cmd` ラッパーを使ってください。

```cmd
cd solution3
run.cmd
```

一時的に PowerShell 側で回避するなら、現在のシェルだけ実行ポリシーを緩めれば足ります。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\run.ps1
```

## 設定

`.env` の主な項目:

- `OCR_DPI`: PDF を画像化する解像度。既定 `300`
- `OCR_CONFIDENCE_THRESHOLD`: 要確認閾値。既定 `0.75`
- `PADDLEOCR_LANG`: 既定 `japan`
- `OLLAMA_BASE_URL`: 既定 `http://127.0.0.1:11434`
- `OLLAMA_MODEL`: 既定 `qwen2.5:7b`

## 画面 / API

- `/` 取込と一覧
- `/vouchers/{voucher_id}` レビューと出力
- `/api/v1/vouchers` 一覧取得
- `/api/v1/vouchers/{voucher_id}` 詳細取得

## 実装上の前提

- OCR は `PaddleOCR` を単一エンジンとして使用
- 非同期ジョブは MVP として `FastAPI BackgroundTasks` で実装
- 伝票テンプレート管理は、まずコード内のラベル辞書ベース
- 抽出精度が不十分な項目は `REVIEW_REQUIRED` に落とす方針
- LLM は正規化支援のみで、OCR に無い情報を埋めない前提

## 運用上の注意

- `PaddleOCR` / `paddlepaddle` の Windows 導入可否は環境差があるため、セットアップ時に失敗した場合は公式の CPU wheel 手順に合わせて再導入してください。
- 現在のセットアップは PaddleOCR 公式インストールページと FAQ に合わせ、`paddlepaddle==3.2.0` / `paddleocr==3.2.0` を前提にしています。これは公式 docs の CPU pip 例と、依存衝突時に特定バージョンを使う FAQ を踏まえた構成です。
- `Ollama` が起動していない場合でもアプリは動きますが、LLM 正規化はスキップされます。
- `runtime/app.db` に SQLite が作られ、原本は `runtime/uploads`、出力は `runtime/exports` に保存されます。

## 今後の拡張候補

- テンプレート定義 UI
- 承認段階の条件分岐
- freee / マネーフォワード向けコネクタ
- ジョブキューの外出し
- 監査ログの WORM / ハッシュチェーン化
