"""Azure Functions エントリポイント。

FastMCP を Streamable HTTP (ASGI) モードで実行し、
BearerAuthMiddleware で保護する。

環境変数:
    MEMORY_STORAGE_BACKEND   : "azure" を設定すること（必須）
    AZURE_STORAGE_CONNECTION_STRING : Azure Storage 接続文字列（必須）
    AZURE_TABLE_NAME         : テーブル名（デフォルト: "memories"）
    MEMORY_MCP_API_KEY       : Bearer トークン（未設定の場合は認証スキップ）
"""
from __future__ import annotations

import os

import azure.functions as func
from dotenv import load_dotenv

load_dotenv()  # ローカルテスト用 (.env)。Azure Functions では App Settings が優先される。

# memory_mcp.py の FastMCP インスタンス（ツール定義を含む）をインポート
# ※ インポート時に MEMORY_STORAGE_BACKEND / AZURE_STORAGE_CONNECTION_STRING が読まれる
from memory_mcp import mcp  # noqa: E402

from auth_middleware import BearerAuthMiddleware  # noqa: E402

# ------------------------------------------------------------------
# ASGI アプリ取得
# ------------------------------------------------------------------
# FastMCP (mcp >= 1.0) の Streamable HTTP ASGI アプリを取得する。
# API が変わった場合は以下を確認:
#   - mcp.streamable_http_app()      ... 標準 API
#   - mcp.get_asgi_app()             ... 旧名
#   - mcp.sse_app()                  ... SSE トランスポート (非推奨)
_asgi_app = mcp.streamable_http_app()

# ------------------------------------------------------------------
# Bearer 認証ミドルウェアを適用
# ------------------------------------------------------------------
_api_key = os.getenv("MEMORY_MCP_API_KEY", "")
if _api_key:
    _asgi_app = BearerAuthMiddleware(_asgi_app, token=_api_key)
else:
    import logging
    logging.warning(
        "MEMORY_MCP_API_KEY is not set — the MCP endpoint is unprotected. "
        "Set this environment variable in Azure Functions App Settings."
    )

# ------------------------------------------------------------------
# Azure Functions アプリ登録
# ------------------------------------------------------------------
# AuthLevel.ANONYMOUS: Azure Functions のキー認証を無効化し、
# アプリ層の Bearer 認証のみを使う。
app = func.AsgiFunctionApp(app=_asgi_app, http_auth_level=func.AuthLevel.ANONYMOUS)
