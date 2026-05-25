"""AzureTableStorage — Azure Table Storage バックエンド。"""
from __future__ import annotations

from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

from storage import MemoryStorage

_PARTITION_KEY = "memories"


def _entity_to_dict(entity: dict[str, Any]) -> dict[str, Any]:
    """Table Storage エンティティをメモリ辞書に変換する（メタフィールドを除去）。"""
    return {
        "content": entity["content"],
        "created_at": entity["created_at"],
        "updated_at": entity["updated_at"],
    }


class AzureTableStorage(MemoryStorage):
    """Azure Table Storage を使ったメモリバックエンド。

    Parameters
    ----------
    connection_string:
        Azure Storage アカウントの接続文字列。
        例: "DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;"
    table_name:
        使用するテーブル名（デフォルト: "memories"）。
    """

    def __init__(self, connection_string: str, table_name: str = "memories") -> None:
        service = TableServiceClient.from_connection_string(connection_string)
        self._client = service.get_table_client(table_name)
        # テーブルが存在しない場合は作成
        try:
            self._client.create_table()
        except Exception:
            # 既に存在する場合は無視
            pass

    # ------------------------------------------------------------------
    # MemoryStorage 実装
    # ------------------------------------------------------------------

    def list_all(self) -> dict[str, dict[str, Any]]:
        entities = self._client.list_entities(
            select=["RowKey", "content", "created_at", "updated_at"]
        )
        result: dict[str, dict[str, Any]] = {}
        for entity in entities:
            key = entity["RowKey"]
            result[key] = _entity_to_dict(entity)
        return result

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            entity = self._client.get_entity(
                partition_key=_PARTITION_KEY, row_key=key
            )
            return _entity_to_dict(entity)
        except ResourceNotFoundError:
            return None

    def create(self, key: str, content: str, now: str) -> dict[str, Any]:
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": key,
            "content": content,
            "created_at": now,
            "updated_at": now,
        }
        self._client.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)
        return _entity_to_dict(entity)

    def update(self, key: str, content: str, created_at: str, now: str) -> dict[str, Any]:
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": key,
            "content": content,
            "created_at": created_at,
            "updated_at": now,
        }
        self._client.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)
        return _entity_to_dict(entity)

    def delete(self, key: str) -> dict[str, Any]:
        # 削除前にエンティティを取得して返す
        entity = self._client.get_entity(
            partition_key=_PARTITION_KEY, row_key=key
        )
        deleted = _entity_to_dict(entity)
        self._client.delete_entity(partition_key=_PARTITION_KEY, row_key=key)
        return deleted

    def rename(self, old_key: str, new_key: str) -> dict[str, Any]:
        # Azure Table Storage は RowKey を直接変更できないため、
        # 新しいキーでエンティティを作成し、古いエンティティを削除する。
        try:
            self._client.get_entity(partition_key=_PARTITION_KEY, row_key=new_key)
            raise KeyError(f"key already exists: {new_key}")
        except ResourceNotFoundError:
            pass  # new_key が存在しないことを確認
        old_entity = self._client.get_entity(partition_key=_PARTITION_KEY, row_key=old_key)
        new_entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": new_key,
            "content": old_entity["content"],
            "created_at": old_entity["created_at"],
            "updated_at": old_entity["updated_at"],
        }
        self._client.upsert_entity(entity=new_entity, mode=UpdateMode.REPLACE)
        self._client.delete_entity(partition_key=_PARTITION_KEY, row_key=old_key)
        return _entity_to_dict(new_entity)
