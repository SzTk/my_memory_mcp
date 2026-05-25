# memory-mcp

会話をまたいで記憶を保持する **MCP（Model Context Protocol）サーバー**。

ストレージに Azure Table Storage を使い、2つの接続方法を提供する。

- **stdio モード** — Claude Code（ローカル）から直接接続
- **HTTP モード** — Azure Functions 上で動作し、Claude.ai・Claude Desktop・Perplexity 等からリモート接続

> ✅ **動作確認済み**: Claude.ai（Web）・Perplexity（Web）・Claude Desktop

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
Claude.ai / Perplexity 等       Azure Table Storage
      │                          (stmemorymcp)
      ▼                          ├─ memories テーブル（メモリデータ）
OAuth 2.1 + PKCE                 └─ oauthdata テーブル（OAuthトークン等）
      │
      ▼
function_app.py
(Azure Functions: memory-mcp-functions)
```

---

## MCP ツール一覧

| ツール名 | 引数 | 説明 |
|---|---|---|
| `list_memory` | なし | 保存されている全メモリを一覧表示 |
| `create_memory` | `content: str`, `key?: str` | 新しいメモリを作成。`key` を省略すると自動生成 |
| `read_memory` | `key: str` | 指定キーのメモリを取得 |
| `update_memory` | `key: str`, `content: str` | 既存メモリの内容を更新（`created_at` 保持） |
| `delete_memory` | `key: str` | 指定キーのメモリを削除 |
| `rename_memory` | `old_key: str`, `new_key: str` | キー名を変更（内容・`created_at` はそのまま） |

### キー名について

`create_memory` の `key` パラメータおよび `rename_memory` の `new_key` には任意の名前を指定できる。

```
// キー名を指定して作成
create_memory(content="Pythonが好き", key="preference_python")

// 自動生成（従来通り）
create_memory(content="Pythonが好き")
→ key: memory_20260525123456_abc123
```

**キー名の制約**（Azure Table Storage の仕様）:
- `/` `\` `#` `?` は使用不可
- 前後の空白不可
- 最大 1024 バイト

---

## ストレージ構造（Azure Table Storage）

### memories テーブル（メモリデータ）

| フィールド | 値 |
|---|---|
| PartitionKey | `memories`（固定） |
| RowKey | キー名（自動生成または指定値） |
| content | 記憶内容（テキスト） |
| created_at | 作成日時（ISO 8601） |
| updated_at | 最終更新日時（ISO 8601） |

### oauthdata テーブル（OAuth 認証データ）

| PartitionKey | 内容 |
|---|---|
| `client` | DCR 登録済みクライアント |
| `state` | 認可フロー中の一時 state（TTL 10 分） |
| `code` | 認可コード（TTL 5 分・ワンタイム） |
| `token` | 発行済みアクセストークン（TTL 1 時間） |
| `refresh` | リフレッシュトークン（TTL 30 日） |

---

## ファイル構成

```
memory_mcp/
├── memory_mcp.py        # FastMCP stdio サーバー（Claude Code 用）
├── function_app.py      # Azure Functions HTTP エンドポイント（MCP + OAuth）
├── oauth.py             # OAuth 2.1 / Google OIDC ロジック（PKCE・トークン発行）
├── oauth_store.py       # OAuth データストレージ（Azure Table Storage）
├── storage.py           # ストレージ抽象基底クラス（ABC）
├── storage_local.py     # ローカル JSON ファイルバックエンド（開発用）
├── storage_azure.py     # Azure Table Storage バックエンド（本番用）
├── migrate.py           # memory.json → Azure Table Storage 移行スクリプト
├── host.json            # Azure Functions 設定（routePrefix: ""）
├── requirements.txt     # Azure Functions ランタイム用依存パッケージ
├── pyproject.toml       # ローカル開発用依存パッケージ（uv）
├── .python-version      # Python 3.11（Azure Functions 互換）
├── local.settings.json  # ローカル環境変数（git 除外）
└── docs/
    ├── azure-migration.md
    └── memory-mcp-oauth-todo.md
```

---

## セットアップ

### 前提条件

- [uv](https://docs.astral.sh/uv/) がインストール済み
- Azure サブスクリプション（Table Storage + Functions 使用時）
- Google Cloud Console で OAuth 2.0 クライアント ID を作成済み（ウェブアプリケーション）

### ローカル開発環境

```bash
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
    "MEMORY_MCP_API_KEY": "<旧 Bearer トークン（後方互換用・任意）>",
    "GOOGLE_CLIENT_ID": "<Google OAuth クライアント ID>",
    "GOOGLE_CLIENT_SECRET": "<Google OAuth クライアントシークレット>",
    "GOOGLE_REDIRECT_URI": "https://<your-app>.azurewebsites.net/oauth/callback",
    "OAUTH_ALLOWED_EMAIL": "<許可するメールアドレス>",
    "FUNCTIONS_BASE_URL": "https://<your-app>.azurewebsites.net",
    "AZURE_OAUTH_TABLE_NAME": "oauthdata"
  }
}
```

`MEMORY_STORAGE_BACKEND=local` にすると `memory.json` を使うローカルモードで動作する（Azure 不要）。

---

## 認証（OAuth 2.1 + PKCE + Google OIDC）

HTTP エンドポイントは **OAuth 2.1 + PKCE** で保護されている。Claude.ai・Perplexity 等の MCP クライアントは OAuth フローを自動で処理する。

### OAuth フロー

```
Claude.ai / Perplexity
  1. GET /api/mcp → 401 + WWW-Authenticate: Bearer resource_metadata="..."
  2. GET /.well-known/oauth-protected-resource → 認可サーバーの場所を取得
  3. GET /.well-known/oauth-authorization-server → エンドポイント一覧を取得
  4. POST /oauth/register → DCR でクライアント登録（client_id 自動取得）
  5. GET /oauth/authorize → Google ログイン画面へリダイレクト
  6. ユーザーが Google でログイン → /oauth/callback → クライアントに認可コードを返す
  7. POST /oauth/token → PKCE 検証 → アクセストークン発行
  8. GET /api/mcp + Bearer <token> → MCP ツール利用開始
```

### OAuth エンドポイント一覧

| エンドポイント | 役割 |
|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 Protected Resource Metadata |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 Authorization Server Metadata |
| `POST /oauth/register` | RFC 7591 Dynamic Client Registration |
| `GET /oauth/authorize` | 認可エンドポイント（Google へリダイレクト） |
| `GET /oauth/callback` | Google からのコールバック受信 |
| `POST /oauth/token` | トークン発行・リフレッシュ |

### 後方互換（旧 Bearer トークン）

移行期間中は `MEMORY_MCP_API_KEY` を設定しておくことで旧方式も継続して使える。OAuth トークンが優先される。

---

## Claude Code から接続（stdio）

`~/.claude/claude_desktop_config.json` または Claude Code の MCP 設定に追加する。

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

## Claude.ai / Perplexity から接続（HTTP + OAuth）

### Claude.ai

Settings → Connectors → Add custom connector → MCP サーバー URL を入力するだけ。OAuth フローは自動。

```
https://memory-mcp-functions.azurewebsites.net/api/mcp
```

### Perplexity

Settings → MCP Servers（またはカスタムコネクタ）→ MCP サーバー URL を入力するだけ。DCR + OAuth フローは自動。

```
https://memory-mcp-functions.azurewebsites.net/api/mcp
```

トランスポート: **Streamable HTTP** を選択（SSE は非対応）。

### Claude Desktop（旧方式・後方互換）

```json
{
  "mcpServers": {
    "memory-remote": {
      "command": "cmd",
      "args": [
        "/c", "npx", "-y", "mcp-remote",
        "https://memory-mcp-functions.azurewebsites.net/api/mcp",
        "--header",
        "Authorization:Bearer <MEMORY_MCP_API_KEY>"
      ]
    }
  }
}
```

> **Note**: macOS / Linux では `"command": "npx"`, `"args": ["-y", "mcp-remote", ...]` で動作する。

---

## Azure Functions デプロイ

```bash
# Azure リソース作成（初回のみ）
az group create --name rg-memory-mcp --location japaneast
az storage account create --name stmemorymcp --resource-group rg-memory-mcp --sku Standard_LRS
az functionapp create \
  --name memory-mcp-functions --resource-group rg-memory-mcp \
  --storage-account stmemorymcp --consumption-plan-location japaneast \
  --runtime python --runtime-version 3.11 --functions-version 4 --os-type linux

# 環境変数を設定（Azure Portal の Environment variables でも可）
az functionapp config appsettings set --name memory-mcp-functions --resource-group rg-memory-mcp \
  --settings \
  AZURE_STORAGE_CONNECTION_STRING="<接続文字列>" \
  AZURE_TABLE_NAME="memories" \
  MEMORY_STORAGE_BACKEND="azure" \
  GOOGLE_CLIENT_ID="<クライアント ID>" \
  GOOGLE_CLIENT_SECRET="<シークレット>" \
  GOOGLE_REDIRECT_URI="https://memory-mcp-functions.azurewebsites.net/oauth/callback" \
  OAUTH_ALLOWED_EMAIL="<許可メール>" \
  FUNCTIONS_BASE_URL="https://memory-mcp-functions.azurewebsites.net" \
  AZURE_OAUTH_TABLE_NAME="oauthdata"

# デプロイ
func azure functionapp publish memory-mcp-functions
```

---

## データ移行（memory.json → Azure Table Storage）

既存の `memory.json` からデータを移行する場合:

```bash
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
| 認証 | OAuth 2.1 + PKCE + Google OIDC (`PyJWT[crypto]`) |
| ストレージ | Azure Table Storage (`azure-data-tables>=12.5.0`) |
| パッケージ管理 | [uv](https://docs.astral.sh/uv/) |
| Python バージョン | 3.11（Azure Functions 互換） |
