# memory-mcp

会話をまたいで記憶を保持する **MCP（Model Context Protocol）サーバー**。

ストレージに Azure Table Storage を使い、2つの接続方法を提供する。

- **stdio モード** — Claude Code（ローカル）から直接接続
- **HTTP モード** — Azure Functions 上で動作し、Claude Desktop・Perplexity 等からリモート接続

---

## アーキテクチャ

```
Claude Code (stdio)
      │
      ▼
memory_mcp.py  ← FastMCP stdio サーバー
      │
      ▼ azure-data-tables SDK
      └──────────────────────────────┐
                                     ▼
Claude Desktop / Perplexity 等   Azure Table Storage
      │                          (stmemorymcp / memories テーブル)
      ▼                              ▲
mcp-remote (npx)                     │
      │  HTTP + Bearer auth           │
      ▼                              │
function_app.py  ── azure-data-tables SDK ─┘
(Azure Functions: memory-mcp-functions)
```

---

## MCP ツール一覧

| ツール名 | 引数 | 説明 |
|---|---|---|
| `list_memory` | なし | 保存されている全メモリを一覧表示 |
| `create_memory` | `content: str` | 新しいメモリを作成 |
| `read_memory` | `key: str` | 指定キーのメモリを取得 |
| `update_memory` | `key: str, content: str` | 既存メモリを更新（`created_at` 保持） |
| `delete_memory` | `key: str` | 指定キーのメモリを削除 |

---

## ストレージ構造（Azure Table Storage）

| フィールド | 値 |
|---|---|
| PartitionKey | `memories`（固定） |
| RowKey | `memory_YYYYMMDDHHMMSS_xxxxxx`（自動生成） |
| content | 記憶内容（テキスト） |
| created_at | 作成日時（ISO 8601） |
| updated_at | 最終更新日時（ISO 8601） |

---

## ファイル構成

```
memory_mcp/
├── memory_mcp.py        # FastMCP stdio サーバー（Claude Code 用）
├── function_app.py      # Azure Functions HTTP エンドポイント（MCP Streamable HTTP）
├── storage.py           # ストレージ抽象基底クラス（ABC）
├── storage_local.py     # ローカル JSON ファイルバックエンド（開発用）
├── storage_azure.py     # Azure Table Storage バックエンド（本番用）
├── migrate.py           # memory.json → Azure Table Storage 移行スクリプト
├── host.json            # Azure Functions v4 設定
├── requirements.txt     # Azure Functions ランタイム用依存パッケージ
├── pyproject.toml       # ローカル開発用依存パッケージ（uv）
├── .python-version      # Python 3.11（Azure Functions 互換）
├── local.settings.json  # ローカル環境変数（git 除外）
└── docs/
    └── azure-migration.md  # 設計ドキュメント
```

---

## セットアップ

### 前提条件

- [uv](https://docs.astral.sh/uv/) がインストール済み
- Azure サブスクリプション（Table Storage + Functions 使用時）

### ローカル開発環境

```powershell
git clone https://github.com/SzTk/my_memory_mcp.git
cd my_memory_mcp
uv sync
```

### 環境変数

`local.settings.json`（git 除外済み）を作成する。

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "MEMORY_STORAGE_BACKEND": "azure",
    "AZURE_STORAGE_CONNECTION_STRING": "<Azure Storage 接続文字列>",
    "AZURE_TABLE_NAME": "memories",
    "MEMORY_MCP_API_KEY": "<Bearer トークン>"
  }
}
```

`MEMORY_STORAGE_BACKEND=local` にすると `memory.json` を使うローカルモードで動作する（Azure 不要）。

---

## Claude Code から接続（stdio）

`claude_desktop_config.json` または Claude Code の MCP 設定に追加する。

```json
{
  "mcpServers": {
    "memory": {
      "command": "uv",
      "args": ["--directory", "/path/to/memory_mcp", "run", "memory_mcp.py"],
      "env": {
        "MEMORY_STORAGE_BACKEND": "azure",
        "AZURE_STORAGE_CONNECTION_STRING": "<接続文字列>",
        "AZURE_TABLE_NAME": "memories"
      }
    }
  }
}
```

---

## Claude Desktop / Perplexity から接続（HTTP）

[mcp-remote](https://www.npmjs.com/package/mcp-remote) を使ってリモート MCP エンドポイントに接続する。

```json
{
  "mcpServers": {
    "memory-remote": {
      "command": "cmd",
      "args": [
        "/c", "npx", "-y", "mcp-remote",
        "https://memory-mcp-functions.azurewebsites.net/api/mcp",
        "--header",
        "Authorization:Bearer <トークン>"
      ]
    }
  }
}
```

> **Note**: macOS / Linux では `"command": "npx"`, `"args": ["-y", "mcp-remote", ...]` で動作する。  
> Windows は `cmd /c npx ...` のラップが必要（パスにスペースが含まれるため）。

---

## Azure Functions デプロイ

```powershell
# Azure リソース作成（初回のみ）
az group create --name rg-memory-mcp --location japaneast
az storage account create --name stmemorymcp --resource-group rg-memory-mcp --sku Standard_LRS
az storage table create --name memories --account-name stmemorymcp
az functionapp create `
  --name memory-mcp-functions --resource-group rg-memory-mcp `
  --storage-account stmemorymcp --consumption-plan-location japaneast `
  --runtime python --runtime-version 3.11 --functions-version 4 --os-type linux

# 環境変数を設定
az functionapp config appsettings set --name memory-mcp-functions --resource-group rg-memory-mcp `
  --settings AZURE_STORAGE_CONNECTION_STRING="<接続文字列>" AZURE_TABLE_NAME="memories" MEMORY_MCP_API_KEY="<トークン>" MEMORY_STORAGE_BACKEND="azure"

# デプロイ
func azure functionapp publish memory-mcp-functions
```

---

## 認証

HTTP エンドポイントは **Bearer トークン認証**を使用する。

- トークンは Azure Functions の環境変数 `MEMORY_MCP_API_KEY` に設定
- リクエストヘッダー: `Authorization: Bearer <トークン>`
- 不一致の場合: HTTP 401

トークンの生成例（WSL / Linux）:

```bash
openssl rand -base64 32
```

---

## データ移行（memory.json → Azure Table Storage）

既存の `memory.json` からデータを移行する場合:

```powershell
# .env または local.settings.json に接続文字列を設定してから実行
uv run python migrate.py
```

---

## 技術スタック

| コンポーネント | 技術 |
|---|---|
| MCP フレームワーク | [FastMCP](https://github.com/jlowin/fastmcp) (`mcp[cli]>=1.27.1`) |
| HTTP サーバー | Azure Functions Python v2 (`FunctionApp` + `@app.route`) |
| MCP プロトコル | MCP Streamable HTTP (JSON-RPC 2.0 over POST) |
| ストレージ | Azure Table Storage (`azure-data-tables>=12.5.0`) |
| リモート接続ブリッジ | [mcp-remote](https://www.npmjs.com/package/mcp-remote) (npx) |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/) |
| Python バージョン | 3.11（Azure Functions 互換） |
