from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

_INSTRUCTIONS = """
あなたはこのメモリシステムを使って、ユーザーとの会話をまたいで重要な情報を記憶します。

## ツールの使い方
- 会話の最初に `list_memory` を呼び出し、過去に記憶した内容を確認すること
- ユーザーが「覚えておいて」「記憶して」と頼んだとき、または重要な個人情報・好み・事実を話してくれたときは、必ず `create_memory` を呼び出すこと
- すでに存在する記憶の内容が変わったときは `update_memory` で更新すること
- 古くなったり不要になった記憶は `delete_memory` で削除すること

## 重要
ユーザーの質問に答える前に、関連するメモリを必ず参照すること。
ユーザーから何かを覚えるよう頼まれたときは、口頭で「わかりました」と答えるだけでなく、必ず `create_memory` ツールを呼び出すこと。
"""

mcp = FastMCP("local-memory", instructions=_INSTRUCTIONS)

# ------------------------------------------------------------------
# ストレージバックエンド選択
# ------------------------------------------------------------------

_BACKEND = os.getenv("MEMORY_STORAGE_BACKEND", "local")

if _BACKEND == "azure":
    from storage_azure import AzureTableStorage

    _storage = AzureTableStorage(
        connection_string=os.environ["AZURE_STORAGE_CONNECTION_STRING"],
        table_name=os.getenv("AZURE_TABLE_NAME", "memories"),
    )
else:
    from storage_local import LocalFileStorage

    _BASE_DIR = Path(__file__).resolve().parent
    _storage = LocalFileStorage(_BASE_DIR / "memory.json")


# ------------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _generate_key() -> str:
    return f"memory_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------
# MCP ツール（signature・レスポンス形状は変更なし）
# ------------------------------------------------------------------


@mcp.tool()
def list_memory() -> dict[str, Any]:
    """保存されているメモリをすべて一覧表示する。"""
    memories = _storage.list_all()
    items = [
        {"key": key, **value}
        for key, value in sorted(memories.items(), key=lambda x: x[0])
    ]
    return {"count": len(items), "items": items}


@mcp.tool()
def create_memory(content: str) -> dict[str, Any]:
    """新しいメモリを作成する。content には記憶したい内容を書く。"""
    content = content.strip()
    if not content:
        return {"ok": False, "error": "content must not be empty"}

    key = _generate_key()
    now = _now_iso()
    entity = _storage.create(key=key, content=content, now=now)

    return {"ok": True, "key": key, "memory": entity}


@mcp.tool()
def read_memory(key: str) -> dict[str, Any]:
    """指定した key のメモリを読み取る。"""
    entity = _storage.get(key)
    if entity is None:
        return {"ok": False, "error": f"memory not found: {key}"}

    return {"ok": True, "key": key, "memory": entity}


@mcp.tool()
def update_memory(key: str, content: str) -> dict[str, Any]:
    """既存メモリを更新する。created_at は保持し、updated_at のみ更新する。"""
    content = content.strip()
    if not content:
        return {"ok": False, "error": "content must not be empty"}

    existing = _storage.get(key)
    if existing is None:
        return {"ok": False, "error": f"memory not found: {key}"}

    entity = _storage.update(
        key=key,
        content=content,
        created_at=existing["created_at"],
        now=_now_iso(),
    )

    return {"ok": True, "key": key, "memory": entity}


@mcp.tool()
def delete_memory(key: str) -> dict[str, Any]:
    """指定した key のメモリを削除する。"""
    existing = _storage.get(key)
    if existing is None:
        return {"ok": False, "error": f"memory not found: {key}"}

    deleted = _storage.delete(key)
    return {"ok": True, "key": key, "deleted": deleted}


if __name__ == "__main__":
    mcp.run(transport="stdio")
