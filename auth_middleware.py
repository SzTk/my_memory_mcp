"""BearerAuthMiddleware — ASGI 層で Bearer トークン認証を行うミドルウェア。

Azure Functions を AuthLevel.ANONYMOUS に設定した上で、
アプリケーション層で認証を行う。環境変数 MEMORY_MCP_API_KEY にトークンを設定する。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    """Authorization: Bearer <token> ヘッダーを検証する ASGI ミドルウェア。

    Parameters
    ----------
    app:
        ラップする ASGI アプリケーション。
    token:
        有効なトークン文字列。空文字列の場合は認証をスキップ（開発用）。
    """

    def __init__(self, app: "ASGIApp", token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(
        self,
        scope: "Scope",
        receive: "Receive",
        send: "Send",
    ) -> None:
        if scope["type"] == "http" and self._token:
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth_header = headers.get(b"authorization", b"").decode()
            expected = f"Bearer {self._token}"

            if auth_header != expected:
                await self._send_401(scope, receive, send)
                return

        await self._app(scope, receive, send)

    @staticmethod
    async def _send_401(
        scope: "Scope",
        receive: "Receive",
        send: "Send",
    ) -> None:
        body = b'{"error": "Unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                    [b"www-authenticate", b'Bearer realm="memory-mcp"'],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
