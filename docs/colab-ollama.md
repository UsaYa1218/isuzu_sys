# Colab で Ollama を動かす

`solution3` はもともと `OLLAMA_BASE_URL` を切り替えられるため、LLM だけを Colab 側へ逃がせます。  
この構成では OCR と Web アプリはローカル PC、LLM 推論だけを Colab Runtime に載せます。

## 前提

- Google Colab の GPU Runtime を使う
- このリポジトリを Colab 側でも clone する
- ローカルの `solution3` は通常通り起動できる

## Colab 側

Colab の新規ノートブックで次を順に実行します。

```python
!git clone https://github.com/Takeru-Mine/solution3.git
%cd solution3
```

その後 `colab/start_ollama_colab.py` を開き、Interactive Window で `#%%` セルを上から順に実行します。  
モデルを変える場合は最初の設定セルの `MODEL` を変更してください。

実行が成功すると、最後に JSON が出ます。`public_base_url` を控えてください。

```json
{
  "model": "qwen2.5:14b",
  "public_base_url": "https://xxxxx.trycloudflare.com",
  "next_env": {
    "OLLAMA_BASE_URL": "https://xxxxx.trycloudflare.com",
    "OLLAMA_MODEL": "qwen2.5:14b"
  }
}
```

### モデル例

- `qwen2.5:14b`
- `qwen3:14b`
- `qwen3:30b`
- `qwen2.5:32b`

初回は `ollama pull` ぶん時間がかかります。

## ローカル側

ローカルの `.env` を更新します。

```env
OLLAMA_BASE_URL=https://xxxxx.trycloudflare.com
OLLAMA_MODEL=qwen2.5:14b
```

必要なら生成オプションも追加できます。

```env
OLLAMA_GENERATE_OPTIONS_JSON={"num_ctx":8192}
```

その後、通常通り起動します。

```cmd
cmd /c run.cmd
```

## 補足

- `OLLAMA_HEADERS_JSON` を使うと、将来的に認証付きプロキシ越しの接続にも対応できます。
- `OLLAMA_GENERATE_OPTIONS_JSON` は Ollama API の `options` にそのまま渡します。`qwen3` 系の調整を入れたいときはここを使います。
- `cloudflared` の Quick Tunnel は開発向けです。URL は毎回変わります。
- Colab Runtime が切れるとトンネルも消えるため、そのたびに `OLLAMA_BASE_URL` は更新が必要です。
