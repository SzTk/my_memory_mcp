"""MemoryStorage — ストレージバックエンドの抽象基底クラス。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryStorage(ABC):
    """メモリの CRUD 操作を定義する抽象インターフェース。"""

    @abstractmethod
    def list_all(self) -> dict[str, dict[str, Any]]:
        """全メモリを {key: {content, created_at, updated_at}} の形式で返す。"""

    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None:
        """指定 key のメモリを返す。存在しない場合は None。"""

    @abstractmethod
    def create(self, key: str, content: str, now: str) -> dict[str, Any]:
        """新しいメモリを保存し、保存したエンティティを返す。"""

    @abstractmethod
    def update(self, key: str, content: str, created_at: str, now: str) -> dict[str, Any]:
        """既存メモリの content と updated_at を更新し、更新後エンティティを返す。"""

    @abstractmethod
    def delete(self, key: str) -> dict[str, Any]:
        """指定 key のメモリを削除し、削除したエンティティを返す。"""

    @abstractmethod
    def rename(self, old_key: str, new_key: str) -> dict[str, Any]:
        """old_key のメモリを new_key に改名し、改名後のエンティティを返す。
        new_key が既に存在する場合は KeyError を送出する。"""
