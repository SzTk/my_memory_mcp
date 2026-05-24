"""LocalFileStorage — memory.json を使ったローカルファイルバックエンド。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from storage import MemoryStorage


class LocalFileStorage(MemoryStorage):
    """メモリを JSON ファイルに保存する実装。オフライン開発・fallback 用。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # MemoryStorage 実装
    # ------------------------------------------------------------------

    def list_all(self) -> dict[str, dict[str, Any]]:
        return self._load()

    def get(self, key: str) -> dict[str, Any] | None:
        return self._load().get(key)

    def create(self, key: str, content: str, now: str) -> dict[str, Any]:
        memories = self._load()
        entity = {"content": content, "created_at": now, "updated_at": now}
        memories[key] = entity
        self._save(memories)
        return entity

    def update(self, key: str, content: str, created_at: str, now: str) -> dict[str, Any]:
        memories = self._load()
        entity = {"content": content, "created_at": created_at, "updated_at": now}
        memories[key] = entity
        self._save(memories)
        return entity

    def delete(self, key: str) -> dict[str, Any]:
        memories = self._load()
        entity = memories.pop(key)
        self._save(memories)
        return entity
