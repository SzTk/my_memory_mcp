# memory-mcp-functions OAuth 2.1 対応 TODO

## 背景・前提

- **現状**: Azure Functions (`function_app.py`) で Bearer トークン（静的）認証
- **目標**: OAuth 2.1 + PKCE + Google OAuth 対応に移行
- **対象クライアント**: Claude Desktop / Claude モバイルアプリ / claude.ai Web
- **個人利用**: 単一ユーザー（自分のみ）

## 重要な仕様（実装前に確認すること）

- Claude のコールバック URL: `https://claude.ai/api/mcp/auth_callback`
- Claude Code のコールバック URL: `http://localhost/callback` および `http://127.0.0.1/callback`（ポート番号不定）
- PKCE（S256）必須
- Protected Resource Metadata（RFC 9728）のホスティング必須
- クライアント登録方式: DCR（Dynamic Client Registration）を採用

---

## PHASE 1: 事前準備

### [ ] 1-1. 別リポジトリの Google OAuth 実装を確認する
- 認可エンドポイント、トークンエンドポイント、コールバック処理を把握する
- DCR（動的クライアント登録）に対応しているか確認する
- PKCE（S256）に対応しているか確認する

### [ ] 1-2. Google Cloud Console の設定
- 既存の OAuth 2.0 クライアント ID に以下のリダイレクト URI を追加する
  - `https://claude.ai/api/mcp/auth_callback`
  - `http://localhost/callback`
  - `http://127.0.0.1/callback`
- スコープ: `openid email profile`（最小限）

### [x] 1-3. Azure Functions の現在の構成を確認する
- `function_app.py` の全エンドポイントを把握する
- `host.json` / `requirements.txt` を確認する
- Azure Functions のプラン（Consumption / Flex 等）を確認する

---

## PHASE 2: Azure Functions にエンドポイントを追加

### [x] 2-1. `/.well-known/oauth-protected-resource` エンドポイントを実装する

```json
{
  "resource": "https://memory-mcp-functions.azurewebsites.net",
  "authorization_servers": ["https://accounts.google.com"],
  "scopes_supported": ["openid", "email", "profile"],
  "bearer_methods_supported": ["header"]
}
```

### [x] 2-2. 既存の MCP エンドポイントに 401 チャレンジを追加する

認証なしリクエストに対して以下を返す:
```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://memory-mcp-functions.azurewebsites.net/.well-known/oauth-protected-resource"
```

### [x] 2-3. DCR エンドポイント（`/oauth/register`）を実装する

Claude がクライアント登録に使用するエンドポイント。
受け取ったクライアント情報を Azure Table Storage 等に保存する。

```python
# 受け取るパラメータ例
{
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "client_name": "Claude",
  "token_endpoint_auth_method": "none"  # public client
}
# 返すパラメータ
{
  "client_id": "<生成したID>",
  "client_secret": "<生成したシークレット（任意）>",
  "redirect_uris": [...]
}
```

### [x] 2-4. 認可エンドポイント（`/oauth/authorize`）を実装する

Claude からの認可リクエストを Google の認可エンドポイントにリダイレクトする。

受け取るパラメータ:
- `client_id`, `redirect_uri`, `response_type=code`
- `code_challenge`, `code_challenge_method=S256`（PKCE）
- `scope`, `state`

Google の認可 URL に `state` を引き継いでリダイレクトする。

### [x] 2-5. コールバックエンドポイント（`/oauth/callback`）を実装する

Google からのコールバックを受け取り、認可コードを一時保存して Claude にリダイレクトする。

```
GET /oauth/callback?code=<google_code>&state=<state>
→ Redirect to https://claude.ai/api/mcp/auth_callback?code=<local_code>&state=<state>
```

### [x] 2-6. トークンエンドポイント（`/oauth/token`）を実装する

Claude が認可コードをアクセストークンに交換するエンドポイント。

- PKCE の `code_verifier` を検証する（S256: `SHA256(code_verifier) == code_challenge`）
- Google のトークンエンドポイントで Google アクセストークンを取得する
- Google トークンから `sub`（ユーザー ID）を取得して自分のトークンを発行する
- 単一ユーザーの場合は自分の `sub` のみ許可するバリデーションを入れる

### [x] 2-7. `/.well-known/oauth-authorization-server` エンドポイントを実装する

Claude が認可サーバーのメタデータを発見するためのエンドポイント。

```json
{
  "issuer": "https://memory-mcp-functions.azurewebsites.net",
  "authorization_endpoint": "https://memory-mcp-functions.azurewebsites.net/oauth/authorize",
  "token_endpoint": "https://memory-mcp-functions.azurewebsites.net/oauth/token",
  "registration_endpoint": "https://memory-mcp-functions.azurewebsites.net/oauth/register",
  "scopes_supported": ["openid", "email", "profile"],
  "response_types_supported": ["code"],
  "code_challenge_methods_supported": ["S256"]
}
```

---

## PHASE 3: MCP エンドポイントのトークン検証を更新する

### [x] 3-1. Bearer トークン検証ロジックを新方式に切り替える

- 旧: 静的 Bearer トークンと比較
- 新: 発行済みアクセストークン（Azure Table Storage に保存）と照合
- 単一ユーザー許可チェックを維持する

### [x] 3-2. トークンの有効期限・リフレッシュ処理を実装する

- アクセストークン: 1 時間（推奨）
- リフレッシュトークン: 30 日（推奨）
- Claude は 401 を受け取ると自動でリフレッシュを試みる

---

## PHASE 4: ローカルテスト

### [ ] 4-1. ngrok でローカル Azure Functions を公開する

```bash
ngrok http 7071
```

### [ ] 4-2. Claude.ai の Settings > Connectors でカスタムコネクタを追加する

- MCP サーバー URL: `https://<ngrok-id>.ngrok-free.app/api/mcp`
- Advanced settings > Client ID / Client Secret を入力する

### [ ] 4-3. OAuth フローを通しでテストする

- Claude が 401 → メタデータ発見 → 認可 → トークン取得 → ツール呼び出し の流れを確認する

---

## PHASE 5: Azure へデプロイ・本番切り替え

### [ ] 5-1. Azure Functions にデプロイする

```bash
func azure functionapp publish memory-mcp-functions
```

### [ ] 5-2. Claude Desktop の接続設定を更新する

旧設定（`mcp-remote` + `--header Authorization:Bearer <token>`）を削除し、
新設定（コネクタ経由 OAuth）に切り替える。

### [ ] 5-3. モバイルアプリ（claude.ai）でも動作確認する

---

## 参考リンク

- [Claude Connector OAuth 仕様（2026年5月）](https://sunpeak.ai/blogs/claude-connector-oauth-authentication)
- [MCP Authorization Specification](https://modelcontextprotocol.io/specification/draft/basic/authorization)
- [RFC 9728: Protected Resource Metadata](https://datatracker.ietf.org/doc/html/rfc9728)
- [RFC 7591: Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591)

## 注意事項

- **個人利用のため、Google ログインを自分以外が成功した場合は 403 を返すバリデーションを必ず入れること**
- Azure Table Storage への接続は既存の `azure-data-tables` SDK をそのまま使用する
- 旧 Bearer トークン方式は移行完了まで並行動作させること（切り戻し用）
