"""migrate.py — memory.json のデータを Azure Table Storage に移行する一時スクリプト。

使い方:
    $env:AZURE_STORAGE_CONNECTION_STRING = "<接続文字列>"
    uv run python migrate.py

実行後はこのスクリプトを削除して構わない。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from azure.data.tables import TableServiceClient

_MEMORY_FILE = Path(__file__).resolve().parent / "memory.json"
_PARTITION_KEY = "memories"


def main() -> None:
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        print("ERROR: AZURE_STORAGE_CONNECTION_STRING が設定されていません。", file=sys.stderr)
        sys.exit(1)

    if not _MEMORY_FILE.exists():
        print(f"INFO: {_MEMORY_FILE} が見つかりません。移行するデータがありません。")
        return

    try:
        data: dict = json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: memory.json の読み込みに失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict) or not data:
        print("INFO: memory.json にデータがありません。")
        return

    table_name = os.getenv("AZURE_TABLE_NAME", "memories")
    service = TableServiceClient.from_connection_string(conn_str)
    table = service.get_table_client(table_name)

    # テーブルが存在しない場合は作成
    try:
        table.create_table()
        print(f"INFO: テーブル '{table_name}' を作成しました。")
    except Exception:
        print(f"INFO: テーブル '{table_name}' は既に存在します。")

    success, failed = 0, 0
    for key, mem in data.items():
        try:
            entity = {
                "PartitionKey": _PARTITION_KEY,
                "RowKey": key,
                "content": mem["content"],
                "created_at": mem["created_at"],
                "updated_at": mem["updated_at"],
            }
            table.upsert_entity(entity)
            print(f"  ✓ {key}")
            success += 1
        except Exception as e:
            print(f"  ✗ {key}: {e}", file=sys.stderr)
            failed += 1

    print(f"\n完了: {success} 件移行, {failed} 件失敗")


if __name__ == "__main__":
    main()
