"""Azure Functions エントリポイント。

AsgiFunctionApp を使わず、MCP Streamable HTTP プロトコルを
Azure Functions の標準 HTTP トリガーで直接実装する。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import azure.functions as func
from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------
# ストレージバックエンド初期化
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
        "description": "新しいメモリを作成する。content には記憶したい内容を書く。",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "記憶したい内容"}},
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
]

# ------------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _generate_key() -> str:
    return f"memory_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _call_tool(name: str, args: dict) -> list[dict]:
    """ツールを呼び出し、MCP content リストを返す。"""
    if name == "list_memory":
        memories = _storage.list_all()
        items = [{"key": k, **v} for k, v in sorted(memories.items())]
        result = {"count": len(items), "items": items}

    elif name == "create_memory":
        content = args.get("content", "").strip()
        if not content:
            result = {"ok": False, "error": "content must not be empty"}
        else:
            key = _generate_key()
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


# ------------------------------------------------------------------
# Azure Functions アプリ
# ------------------------------------------------------------------

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="mcp", methods=["GET", "POST", "DELETE"])
def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    """MCP Streamable HTTP エンドポイント。"""

    # Bearer 認証
    api_key = os.getenv("MEMORY_MCP_API_KEY", "")
    if api_key:
        auth = req.headers.get("Authorization", "")
        if auth != f"Bearer {api_key}":
            return func.HttpResponse(
                '{"error": "Unauthorized"}',
                status_code=401,
                mimetype="application/json",
                headers={"WWW-Authenticate": 'Bearer realm="memory-mcp"'},
            )

    # GET: SSE 未対応の旨を返す
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

    logging.info("MCP method: %s", method)

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memory-mcp", "version": "0.1.0"},
            "instructions": (
                "このメモリシステムを使って会話をまたいで重要な情報を記憶します。"
                "会話の最初に list_memory を呼び出し、過去の記憶を確認すること。"
            ),
        })

    elif method in ("notifications/initialized", "initialized"):
        # クライアントからの通知 → 202 で応答
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
            logging.exception("Tool call error: %s", tool_name)
            return _error(req_id, -32603, f"Internal error: {exc}", status=500)

    else:
        return _error(req_id, -32601, f"Method not found: {method}")
