"""Azure Functions エントリポイント。

OAuth 2.1 + PKCE + Google OIDC に対応した MCP Streamable HTTP サーバー。

エンドポイント一覧（routePrefix = "" のため /api プレフィックスなし）:
  POST/GET  /api/mcp                                    ← MCP エンドポイント（既存・後方互換）
  GET       /.well-known/oauth-protected-resource        ← RFC 9728 リソースメタデータ
  GET       /.well-known/oauth-authorization-server      ← RFC 8414 認可サーバーメタデータ
  POST      /oauth/register                              ← DCR (RFC 7591)
  GET       /oauth/authorize                             ← 認可エンドポイント
  GET       /oauth/callback                              ← Google コールバック
  POST      /oauth/token                                 ← トークンエンドポイント
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import azure.functions as func
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# メモリストレージ初期化
# ------------------------------------------------------------------
_BACKEND = os.getenv("MEMORY_STORAGE_BACKEND", "local")

if _BACKEND == "azure":
    from storage_azure import AzureTableStorage
    _storage = AzureTableStorage(
        connection_string=os.environ["AZURE_STORAGE_CONNECTION_STRING"],
        table_name=os.getenv("AZURE_TABLE_NAME", "memories"),
    )
else:
    from pathlib import Path
    from storage_local import LocalFileStorage
    _storage = LocalFileStorage(Path(__file__).parent / "memory.json")

# ------------------------------------------------------------------
# OAuth ストレージ初期化
# ------------------------------------------------------------------
_OAUTH_ENABLED = bool(os.getenv("GOOGLE_CLIENT_ID"))

_oauth_store = None
if _OAUTH_ENABLED:
    try:
        from oauth_store import OAuthStore
        _oauth_store = OAuthStore(
            connection_string=os.environ["AZURE_STORAGE_CONNECTION_STRING"],
            table_name=os.getenv("AZURE_OAUTH_TABLE_NAME", "oauthdata"),
        )
        from oauth import (
            build_google_auth_url,
            exchange_google_code,
            generate_token,
            get_google_user_info,
            verify_pkce_s256,
        )
    except Exception as _e:
        logger.warning("OAuth store initialization failed: %s", _e)
        _OAUTH_ENABLED = False

# ------------------------------------------------------------------
# 設定値
# ------------------------------------------------------------------
_FUNCTIONS_BASE_URL = os.getenv(
    "FUNCTIONS_BASE_URL", "https://memory-mcp-functions.azurewebsites.net"
).rstrip("/")

_GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
# Google Console に登録するコールバック URL
_GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    f"{_FUNCTIONS_BASE_URL}/oauth/callback",
)
# 許可するユーザーのメールアドレス（個人利用: 自分のみ）
_ALLOWED_EMAIL = os.getenv("OAUTH_ALLOWED_EMAIL", "")

# 後方互換: 静的 Bearer トークン（移行期間中は並行動作）
_LEGACY_API_KEY = os.getenv("MEMORY_MCP_API_KEY", "")

# ------------------------------------------------------------------
# MCP ツール定義（スキーマ）
# ------------------------------------------------------------------
_TOOLS = [
    {
        "name": "list_memory",
        "description": "保存されているメモリをすべて一覧表示する。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_memory",
        "description": (
            "新しいメモリを作成する。content には記憶したい内容を書く。"
            "key を指定すると任意のキー名で保存できる（省略時は自動生成）。"
            "key に使える文字: 英数字・アンダースコア・ハイフン・ドット（/ \\ # ? は不可）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "記憶したい内容"},
                "key": {"type": "string", "description": "キー名（省略時は自動生成）"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "read_memory",
        "description": "指定した key のメモリを読み取る。",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "メモリのキー"}},
            "required": ["key"],
        },
    },
    {
        "name": "update_memory",
        "description": "既存メモリを更新する。created_at は保持し、updated_at のみ更新する。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "メモリのキー"},
                "content": {"type": "string", "description": "新しい内容"},
            },
            "required": ["key", "content"],
        },
    },
    {
        "name": "delete_memory",
        "description": "指定した key のメモリを削除する。",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "削除するメモリのキー"}},
            "required": ["key"],
        },
    },
    {
        "name": "rename_memory",
        "description": "既存メモリのキー名を変更する。内容・作成日時はそのまま保持される。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_key": {"type": "string", "description": "現在のキー名"},
                "new_key": {"type": "string", "description": "新しいキー名"},
            },
            "required": ["old_key", "new_key"],
        },
    },
]

# ------------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _generate_memory_key() -> str:
    return f"memory_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


# Azure Table Storage RowKey として使えない文字
_KEY_FORBIDDEN = set('/\\#?')


def _validate_key(key: str) -> str | None:
    """キー名を検証する。問題があればエラーメッセージを返す。問題なければ None。"""
    if not key:
        return "key must not be empty"
    if key != key.strip():
        return "key must not have leading/trailing whitespace"
    if any(c in _KEY_FORBIDDEN for c in key):
        return "key must not contain / \\ # ?"
    if len(key.encode()) > 1024:
        return "key is too long (max 1024 bytes)"
    return None


def _call_tool(name: str, args: dict) -> list[dict]:
    """ツールを呼び出し、MCP content リストを返す。"""
    if name == "list_memory":
        memories = _storage.list_all()
        items = [{"key": k, **v} for k, v in sorted(memories.items())]
        result = {"count": len(items), "items": items}

    elif name == "create_memory":
        content = args.get("content", "").strip()
        custom_key = args.get("key", "").strip()
        if not content:
            result = {"ok": False, "error": "content must not be empty"}
        elif custom_key:
            err = _validate_key(custom_key)
            if err:
                result = {"ok": False, "error": f"invalid key: {err}"}
            elif _storage.get(custom_key) is not None:
                result = {"ok": False, "error": f"key already exists: {custom_key}"}
            else:
                entity = _storage.create(key=custom_key, content=content, now=_now_iso())
                result = {"ok": True, "key": custom_key, "memory": entity}
        else:
            key = _generate_memory_key()
            entity = _storage.create(key=key, content=content, now=_now_iso())
            result = {"ok": True, "key": key, "memory": entity}

    elif name == "read_memory":
        key = args.get("key", "")
        entity = _storage.get(key)
        result = (
            {"ok": True, "key": key, "memory": entity}
            if entity is not None
            else {"ok": False, "error": f"memory not found: {key}"}
        )

    elif name == "update_memory":
        key = args.get("key", "")
        content = args.get("content", "").strip()
        if not content:
            result = {"ok": False, "error": "content must not be empty"}
        else:
            existing = _storage.get(key)
            if existing is None:
                result = {"ok": False, "error": f"memory not found: {key}"}
            else:
                entity = _storage.update(
                    key=key,
                    content=content,
                    created_at=existing["created_at"],
                    now=_now_iso(),
                )
                result = {"ok": True, "key": key, "memory": entity}

    elif name == "delete_memory":
        key = args.get("key", "")
        existing = _storage.get(key)
        if existing is None:
            result = {"ok": False, "error": f"memory not found: {key}"}
        else:
            deleted = _storage.delete(key)
            result = {"ok": True, "key": key, "deleted": deleted}

    elif name == "rename_memory":
        old_key = args.get("old_key", "").strip()
        new_key = args.get("new_key", "").strip()
        if not old_key or not new_key:
            result = {"ok": False, "error": "old_key and new_key are required"}
        else:
            err = _validate_key(new_key)
            if err:
                result = {"ok": False, "error": f"invalid new_key: {err}"}
            elif _storage.get(old_key) is None:
                result = {"ok": False, "error": f"memory not found: {old_key}"}
            else:
                try:
                    entity = _storage.rename(old_key=old_key, new_key=new_key)
                    result = {"ok": True, "old_key": old_key, "new_key": new_key, "memory": entity}
                except KeyError:
                    result = {"ok": False, "error": f"key already exists: {new_key}"}

    else:
        result = {"error": f"Unknown tool: {name}"}

    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]


# ------------------------------------------------------------------
# JSON-RPC レスポンスヘルパー
# ------------------------------------------------------------------


def _ok(req_id, result: dict) -> func.HttpResponse:
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)
    return func.HttpResponse(body, status_code=200, mimetype="application/json")


def _error(req_id, code: int, message: str, status: int = 400) -> func.HttpResponse:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )
    return func.HttpResponse(body, status_code=status, mimetype="application/json")


def _oauth_error(error: str, description: str, status: int = 400) -> func.HttpResponse:
    """OAuth エラーレスポンスを返す（RFC 6749 形式）。"""
    body = json.dumps(
        {"error": error, "error_description": description}, ensure_ascii=False
    )
    return func.HttpResponse(body, status_code=status, mimetype="application/json")


def _auth_challenge_response() -> func.HttpResponse:
    """認証が必要なときの 401 レスポンス（WWW-Authenticate ヘッダー付き）。"""
    resource_metadata_url = (
        f"{_FUNCTIONS_BASE_URL}/.well-known/oauth-protected-resource"
    )
    return func.HttpResponse(
        '{"error":"Unauthorized","message":"Authentication required"}',
        status_code=401,
        mimetype="application/json",
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{resource_metadata_url}"'
            )
        },
    )


def _verify_bearer_token(req: func.HttpRequest) -> bool:
    """Bearer トークンを検証する。

    検証順序:
      1. 発行済み OAuth アクセストークン（OAuth フロー有効時）
      2. 静的 API キー（後方互換・移行期間用）

    Returns:
        True: 認証成功 / False: 認証失敗
    """
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False

    token = auth[7:].strip()

    # 1. 発行済み OAuth アクセストークン
    if _OAUTH_ENABLED and _oauth_store:
        token_info = _oauth_store.verify_access_token(token)
        if token_info is not None:
            logger.info("OAuth token verified: user=%s", token_info.get("user_email"))
            return True

    # 2. 静的 API キー（後方互換）
    if _LEGACY_API_KEY and token == _LEGACY_API_KEY:
        return True

    return False


# ------------------------------------------------------------------
# Azure Functions アプリ（routePrefix="" を host.json で設定済み）
# ------------------------------------------------------------------

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# ==================================================================
# MCP エンドポイント（/api/mcp）
# ==================================================================


@app.route(route="api/mcp", methods=["GET", "POST", "DELETE"])
def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    """MCP Streamable HTTP エンドポイント。

    認証が設定されている場合（GOOGLE_CLIENT_ID または MEMORY_MCP_API_KEY）、
    Bearer トークンを検証する。未認証は 401 + WWW-Authenticate を返す。
    """
    # 認証チェック（いずれかが設定されていれば必須）
    auth_required = bool(_OAUTH_ENABLED or _LEGACY_API_KEY)
    if auth_required and not _verify_bearer_token(req):
        return _auth_challenge_response()

    # GET: SSE 未対応
    if req.method == "GET":
        return func.HttpResponse(
            json.dumps({"error": "Use POST for MCP Streamable HTTP"}),
            status_code=405,
            mimetype="application/json",
        )

    # POST: JSON-RPC 処理
    try:
        body = req.get_json()
    except Exception:
        return _error(None, -32700, "Parse error")

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    logger.info("MCP method: %s", method)

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memory-mcp", "version": "0.2.0"},
            "instructions": (
                "このメモリシステムを使って会話をまたいで重要な情報を記憶します。"
                "会話の最初に list_memory を呼び出し、過去の記憶を確認すること。"
            ),
        })

    elif method in ("notifications/initialized", "initialized"):
        return func.HttpResponse("", status_code=202)

    elif method == "ping":
        return _ok(req_id, {})

    elif method == "tools/list":
        return _ok(req_id, {"tools": _TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        try:
            content = _call_tool(tool_name, tool_args)
            return _ok(req_id, {"content": content})
        except Exception as exc:
            logger.exception("Tool call error: %s", tool_name)
            return _error(req_id, -32603, f"Internal error: {exc}", status=500)

    else:
        return _error(req_id, -32601, f"Method not found: {method}")


# ==================================================================
# PHASE 2-1: Protected Resource Metadata（RFC 9728）
# GET /.well-known/oauth-protected-resource
# ==================================================================


@app.route(route=".well-known/oauth-protected-resource", methods=["GET"])
def oauth_protected_resource(req: func.HttpRequest) -> func.HttpResponse:
    """RFC 9728: OAuth 2.0 Protected Resource Metadata。

    Claude はこのエンドポイントで認可サーバーの場所を発見する。
    """
    metadata = {
        "resource": _FUNCTIONS_BASE_URL,
        "authorization_servers": [_FUNCTIONS_BASE_URL],
        "scopes_supported": ["openid", "email", "profile"],
        "bearer_methods_supported": ["header"],
    }
    return func.HttpResponse(
        json.dumps(metadata, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


# ==================================================================
# PHASE 2-7: Authorization Server Metadata（RFC 8414）
# GET /.well-known/oauth-authorization-server
# ==================================================================


@app.route(route=".well-known/oauth-authorization-server", methods=["GET"])
def oauth_authorization_server_metadata(req: func.HttpRequest) -> func.HttpResponse:
    """RFC 8414: OAuth 2.0 Authorization Server Metadata。

    Claude はこのエンドポイントで各 OAuth エンドポイントの URL を取得する。
    """
    metadata = {
        "issuer": _FUNCTIONS_BASE_URL,
        "authorization_endpoint": f"{_FUNCTIONS_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{_FUNCTIONS_BASE_URL}/oauth/token",
        "registration_endpoint": f"{_FUNCTIONS_BASE_URL}/oauth/register",
        "scopes_supported": ["openid", "email", "profile"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    }
    return func.HttpResponse(
        json.dumps(metadata, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


# ==================================================================
# PHASE 2-3: DCR エンドポイント（RFC 7591）
# POST /oauth/register
# ==================================================================


@app.route(route="oauth/register", methods=["POST"])
def oauth_register(req: func.HttpRequest) -> func.HttpResponse:
    """RFC 7591: Dynamic Client Registration。

    Claude が最初に呼び出し、client_id を取得する。
    """
    if not _OAUTH_ENABLED or _oauth_store is None:
        return _oauth_error("server_error", "OAuth not configured", 503)

    try:
        body = req.get_json()
    except Exception:
        return _oauth_error("invalid_request", "Invalid JSON body")

    redirect_uris: list[str] = body.get("redirect_uris", [])
    client_name: str = body.get("client_name", "unknown")
    # public client（client_secret なし）を想定
    token_endpoint_auth_method: str = body.get("token_endpoint_auth_method", "none")

    if not redirect_uris:
        return _oauth_error("invalid_redirect_uri", "redirect_uris is required")

    client_id = generate_token(16)
    # Perplexity 等、public client でも client_secret を要求するクライアントに対応するため
    # token_endpoint_auth_method に関わらず常に client_secret を発行する
    client_secret = generate_token(32)

    _oauth_store.save_client(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uris=redirect_uris,
        client_name=client_name,
    )

    response_body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": token_endpoint_auth_method,
    }

    return func.HttpResponse(
        json.dumps(response_body, ensure_ascii=False),
        status_code=201,
        mimetype="application/json",
    )


# ==================================================================
# PHASE 2-4: 認可エンドポイント
# GET /oauth/authorize
# ==================================================================


@app.route(route="oauth/authorize", methods=["GET"])
def oauth_authorize(req: func.HttpRequest) -> func.HttpResponse:
    """OAuth 2.1 認可エンドポイント。

    Claude からのリクエストを受け取り、Google の認可画面にリダイレクトする。
    Claude からのパラメータ（PKCE, state 等）は一時保存する。
    """
    if not _OAUTH_ENABLED or _oauth_store is None:
        return _oauth_error("server_error", "OAuth not configured", 503)

    p = req.params

    client_id = p.get("client_id", "")
    redirect_uri = p.get("redirect_uri", "")
    response_type = p.get("response_type", "")
    code_challenge = p.get("code_challenge", "")
    code_challenge_method = p.get("code_challenge_method", "")
    state = p.get("state", "")
    # scope は任意（無視して openid email profile を使用）

    # バリデーション
    if response_type != "code":
        return _oauth_error("unsupported_response_type", "Only 'code' is supported")

    if code_challenge_method != "S256":
        return _oauth_error(
            "invalid_request", "code_challenge_method must be S256"
        )

    if not code_challenge:
        return _oauth_error("invalid_request", "code_challenge is required")

    if not redirect_uri:
        return _oauth_error("invalid_redirect_uri", "redirect_uri is required")

    # client_id の検証（登録済みクライアントのみ許可）
    client = _oauth_store.get_client(client_id)
    if client is None:
        return _oauth_error("invalid_client", "Unknown client_id")

    if redirect_uri not in client["redirect_uris"]:
        return _oauth_error(
            "invalid_redirect_uri",
            f"redirect_uri not registered: {redirect_uri}",
        )

    # Google に渡す state（= Claude の state との対応を保存するキー）
    google_state = generate_token(24)

    _oauth_store.save_state(
        google_state,
        original_state=state,
        redirect_uri=redirect_uri,
        client_id=client_id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )

    google_url = build_google_auth_url(
        google_client_id=_GOOGLE_CLIENT_ID,
        google_redirect_uri=_GOOGLE_REDIRECT_URI,
        state=google_state,
    )

    return func.HttpResponse(
        status_code=302,
        headers={"Location": google_url},
    )


# ==================================================================
# PHASE 2-5: コールバックエンドポイント
# GET /oauth/callback
# ==================================================================


@app.route(route="oauth/callback", methods=["GET"])
def oauth_callback(req: func.HttpRequest) -> func.HttpResponse:
    """Google からのコールバックを受け取る。

    Google の認可コードを当サーバーの認可コードに変換し、
    Claude の redirect_uri にリダイレクトする。
    """
    if not _OAUTH_ENABLED or _oauth_store is None:
        return _oauth_error("server_error", "OAuth not configured", 503)

    p = req.params
    google_code = p.get("code", "")
    google_state = p.get("state", "")
    error = p.get("error", "")

    # Google がエラーを返した場合
    if error:
        logger.warning("Google OAuth error: %s", error)
        return func.HttpResponse(
            f"<html><body>Google OAuth error: {error}</body></html>",
            status_code=400,
            mimetype="text/html",
        )

    if not google_code or not google_state:
        return _oauth_error("invalid_request", "Missing code or state from Google")

    # state を pop（一度きり・期限チェック付き）
    state_data = _oauth_store.pop_state(google_state)
    if state_data is None:
        return _oauth_error(
            "invalid_request", "Invalid or expired state. Please retry OAuth flow."
        )

    # Google 認可コードをトークンに交換してユーザー情報を取得
    try:
        google_tokens = exchange_google_code(
            code=google_code,
            google_client_id=_GOOGLE_CLIENT_ID,
            google_client_secret=_GOOGLE_CLIENT_SECRET,
            google_redirect_uri=_GOOGLE_REDIRECT_URI,
        )
        user_sub, user_email = get_google_user_info(google_tokens, _GOOGLE_CLIENT_ID)
    except Exception as exc:
        logger.exception("Google token exchange failed")
        return _oauth_error("server_error", f"Google token exchange failed: {exc}", 502)

    # 個人利用: 許可されたメールアドレスのみ
    if _ALLOWED_EMAIL and user_email != _ALLOWED_EMAIL:
        logger.warning(
            "OAuth denied: unauthorized email=%s (allowed=%s)", user_email, _ALLOWED_EMAIL
        )
        return func.HttpResponse(
            "<html><body><h2>403 Forbidden</h2>"
            "<p>このサービスはご利用いただけません。</p></body></html>",
            status_code=403,
            mimetype="text/html",
        )

    # 当サーバーの認可コードを発行
    auth_code = generate_token(32)
    _oauth_store.save_code(
        auth_code,
        client_id=state_data["client_id"],
        redirect_uri=state_data["redirect_uri"],
        code_challenge=state_data["code_challenge"],
        code_challenge_method=state_data["code_challenge_method"],
        user_sub=user_sub,
        user_email=user_email,
    )

    logger.info("OAuth auth code issued for user=%s", user_email)

    # Claude の redirect_uri にリダイレクト
    redirect_params = {"code": auth_code}
    if state_data["original_state"]:
        redirect_params["state"] = state_data["original_state"]

    redirect_url = (
        state_data["redirect_uri"]
        + "?"
        + urllib.parse.urlencode(redirect_params)
    )

    return func.HttpResponse(
        status_code=302,
        headers={"Location": redirect_url},
    )


# ==================================================================
# PHASE 2-6 / PHASE 3: トークンエンドポイント
# POST /oauth/token
# ==================================================================


@app.route(route="oauth/token", methods=["POST"])
def oauth_token(req: func.HttpRequest) -> func.HttpResponse:
    """OAuth 2.1 トークンエンドポイント。

    対応する grant_type:
      - authorization_code: 認可コード + code_verifier (PKCE) でアクセストークンを発行
      - refresh_token: リフレッシュトークンでトークンをローテート
    """
    if not _OAUTH_ENABLED or _oauth_store is None:
        return _oauth_error("server_error", "OAuth not configured", 503)

    # application/x-www-form-urlencoded を手動パース
    try:
        raw = req.get_body().decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
    except Exception:
        return _oauth_error("invalid_request", "Failed to parse request body")

    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")

    # クライアント検証
    client = _oauth_store.get_client(client_id) if client_id else None
    if client is None:
        return _oauth_error("invalid_client", "Unknown client_id", 401)

    # ---------- authorization_code ----------
    if grant_type == "authorization_code":
        code = form.get("code", "")
        code_verifier = form.get("code_verifier", "")
        redirect_uri = form.get("redirect_uri", "")

        if not code or not code_verifier:
            return _oauth_error(
                "invalid_request", "code and code_verifier are required"
            )

        code_data = _oauth_store.pop_code(code)
        if code_data is None:
            return _oauth_error("invalid_grant", "Invalid or expired authorization code")

        if code_data["client_id"] != client_id:
            return _oauth_error("invalid_grant", "client_id mismatch")

        if redirect_uri and code_data["redirect_uri"] != redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri mismatch")

        # PKCE 検証
        if code_data["code_challenge_method"] == "S256":
            if not verify_pkce_s256(code_verifier, code_data["code_challenge"]):
                return _oauth_error("invalid_grant", "PKCE code_verifier mismatch")

        access_token = generate_token(32)
        refresh_token = generate_token(32)

        _oauth_store.save_access_token(
            access_token,
            refresh_token=refresh_token,
            client_id=client_id,
            user_sub=code_data["user_sub"],
            user_email=code_data["user_email"],
        )

        logger.info("Access token issued for user=%s", code_data["user_email"])

        return func.HttpResponse(
            json.dumps(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": refresh_token,
                    "scope": "openid email profile",
                },
                ensure_ascii=False,
            ),
            status_code=200,
            mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )

    # ---------- refresh_token ----------
    elif grant_type == "refresh_token":
        refresh_token_value = form.get("refresh_token", "")
        if not refresh_token_value:
            return _oauth_error("invalid_request", "refresh_token is required")

        rt_info = _oauth_store.get_refresh_token_info(refresh_token_value)
        if rt_info is None:
            return _oauth_error(
                "invalid_grant", "Invalid or expired refresh token", 401
            )

        if rt_info["client_id"] != client_id:
            return _oauth_error("invalid_client", "client_id mismatch", 401)

        new_access_token = generate_token(32)
        new_refresh_token = generate_token(32)

        _oauth_store.rotate_tokens(
            old_refresh_token=refresh_token_value,
            new_access_token=new_access_token,
            new_refresh_token=new_refresh_token,
            client_id=client_id,
            user_sub=rt_info["user_sub"],
            user_email=rt_info["user_email"],
        )

        logger.info("Tokens rotated for user=%s", rt_info["user_email"])

        return func.HttpResponse(
            json.dumps(
                {
                    "access_token": new_access_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": new_refresh_token,
                    "scope": "openid email profile",
                },
                ensure_ascii=False,
            ),
            status_code=200,
            mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )

    else:
        return _oauth_error(
            "unsupported_grant_type",
            f"Unsupported grant_type: {grant_type}",
        )
