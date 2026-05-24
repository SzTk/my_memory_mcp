# Azure 移行設計ドキュメント

## 概要

`memory_mcp` のストレージをローカル JSON ファイルから Azure Table Storage へ移行し、  
Azure Functions 上で MCP HTTP サーバーとして動作させる。  
Claude Desktop や Perplexity 等の AI アシスタントがリモートから接続できる。

---

## アーキテクチャ

```
Claude Desktop / Perplexity / 他の AI クライアント
          │
          │ HTTP (MCP Streamable HTTP トランスポート)
          │ Authorization: Bearer <token>
          ▼
  Azure Functions App (Python 3.11, Linux, Consumption)
  ┌──────────────────────────────────────┐
  │  BearerAuthMiddleware (ASGI)         │  ← トークン認証
  │  FastMCP (Streamable HTTP / ASGI)    │  ← MCP サーバー本体
  └──────────────────────────────────────┘
          │
          ▼ azure-data-tables SDK
  Azure Table Storage

---（ローカル開発）---

Claude Code (stdio)
     │
     ▼
memory_mcp.py (FastMCP stdio モード)
     │
     ├──→ storage_local.py   (memory.json, MEMORY_STORAGE_BACKEND=local)
     └──→ storage_azure.py   (Azure Table Storage, MEMORY_STORAGE_BACKEND=azure)
```

### トランスポートに Streamable HTTP を選んだ理由

| | SSE（旧） | Streamable HTTP（新・推奨） |
|---|---|---|
| Azure Functions との相性 | ❌ 30秒で接続切断リスク | ✅ 通常の HTTP リクエスト |
| Claude Desktop 対応 | ✅ | ✅ |
| Perplexity 等 MCP クライアント | △ | ✅（MCP 標準仕様） |

### 認証方式

- Azure Functions: `AuthLevel.ANONYMOUS`（Azure 側のキー認証は使わない）
- アプリ層: `Authorization: Bearer <token>` ヘッダーで認証
- トークンは環境変数 `MEMORY_MCP_API_KEY` で管理

---

## プロジェクト構成

```
memory_mcp/
├── memory_mcp.py          # FastMCP ツール定義（stdio / HTTP 共用）
├── function_app.py        # Azure Functions ASGI エントリポイント
├── auth_middleware.py     # Bearer トークン ASGI ミドルウェア
├── storage.py             # MemoryStorage ABC
├── storage_local.py       # ローカル JSON 実装
├── storage_azure.py       # Azure Table Storage 実装
├── migrate.py             # 一時移行スクリプト（実行後削除）
├── host.json              # Azure Functions ホスト設定
├── requirements.txt       # Azure Functions 依存（Python 3.11 用）
├── pyproject.toml         # ローカル開発依存（uv 用）
├── local.settings.json    # ローカルテスト用設定（git 管理外）
├── .env                   # ローカル環境変数（git 管理外）
├── memory.json            # ローカル fallback（git 管理外）
└── docs/
    └── azure-migration.md # このファイル
```

---

## 環境変数

### ローカル開発 (`.env`)

```env
MEMORY_STORAGE_BACKEND=local          # または azure
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_TABLE_NAME=memories
MEMORY_MCP_API_KEY=your-secret-token
```

### Azure Functions App Settings

| 設定名 | 値 |
|---|---|
| `MEMORY_STORAGE_BACKEND` | `azure` |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure Portal から取得 |
| `AZURE_TABLE_NAME` | `memories` |
| `MEMORY_MCP_API_KEY` | 任意のシークレットトークン |

---

## Azure リソース構成

| リソース | 種類 | 用途 |
|---|---|---|
| `rg-memory-mcp` | Resource Group | 全リソース管理 |
| `stmemorymcp` | Storage Account (LRS) | Table Storage + Function App 状態 |
| `memories` | Table | メモリデータ永続化 |
| `memory-mcp-functions` | Function App (Python 3.11) | MCP HTTP サーバー |

**想定月額コスト:** 個人用途 $0〜$2/月

---

## データモデル（Azure Table Storage）

| フィールド | 値 |
|---|---|
| PartitionKey | `"memories"`（固定） |
| RowKey | `memory_YYYYMMDDHHMMSS_xxxxxx` |
| content | メモリ本文（最大 32KB） |
| created_at | ISO 8601 タイムスタンプ |
| updated_at | ISO 8601 タイムスタンプ |

---

## セットアップ手順

### 1. Azure インフラ構築

```powershell
az login
az group create --name rg-memory-mcp --location japaneast

az storage account create `
  --name stmemorymcp `
  --resource-group rg-memory-mcp --sku Standard_LRS

az storage table create --name memories `
  --account-name stmemorymcp

az functionapp create `
  --name memory-mcp-functions `
  --resource-group rg-memory-mcp `
  --storage-account stmemorymcp `
  --consumption-plan-location japaneast `
  --runtime python --runtime-version 3.11 `
  --functions-version 4 --os-type linux
```

### 2. 接続文字列の取得

```powershell
az storage account show-connection-string `
  --name stmemorymcp --resource-group rg-memory-mcp --query connectionString -o tsv
```

→ 取得した値を `local.settings.json` と Azure App Settings に設定する。

### 3. 既存データの移行

```powershell
$env:AZURE_STORAGE_CONNECTION_STRING = "<接続文字列>"
uv run python migrate.py
# 完了後に migrate.py を削除する
```

### 4. 依存パッケージのインストール（ローカル開発）

```powershell
uv sync
```

### 5. ローカルで Azure Functions をテスト

```powershell
# Azure Functions Core Tools が必要
# winget install Microsoft.AzureFunctionsCoreTools

func start
# → http://localhost:7071/mcp でアクセス可能
```

### 6. Azure Functions へのデプロイ

```powershell
# App Settings を設定
az functionapp config appsettings set `
  --name memory-mcp-functions --resource-group rg-memory-mcp `
  --settings `
    MEMORY_STORAGE_BACKEND="azure" `
    AZURE_STORAGE_CONNECTION_STRING="<接続文字列>" `
    AZURE_TABLE_NAME="memories" `
    MEMORY_MCP_API_KEY="<シークレットトークン>"

# デプロイ
func azure functionapp publish memory-mcp-functions --python
```

---

## Claude Desktop への接続設定

`~/AppData/Roaming/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "https://memory-mcp-functions.azurewebsites.net/mcp",
      "headers": {
        "Authorization": "Bearer <MEMORY_MCP_API_KEY の値>"
      }
    }
  }
}
```

### Claude Code（ローカル stdio）への接続設定

`~/AppData/Roaming/Claude/claude_desktop_config.json` または Claude Code の MCP 設定:

```json
{
  "mcpServers": {
    "memory": {
      "command": "uv",
      "args": ["run", "python", "memory_mcp.py"],
      "cwd": "C:\\Users\\takay\\MyGit\\memory_mcp",
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

## 開発ツール（別途インストール）

```powershell
winget install Microsoft.AzureFunctionsCoreTools
winget install Microsoft.AzureCLI
```
