"""OAuth データストレージ（Azure Table Storage）。

単一テーブル（デフォルト: `oauthdata`）にパーティションキーで種別を分けて保存する:
  - PartitionKey="client"   → DCR 登録済みクライアント
  - PartitionKey="state"    → Google OAuth フロー中の一時 state
  - PartitionKey="code"     → 認可コード（短命・ワンタイム）
  - PartitionKey="token"    → 発行済みアクセストークン
  - PartitionKey="refresh"  → リフレッシュトークン（逆引き用）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode

logger = logging.getLogger(__name__)

_PARTITION_CLIENT = "client"
_PARTITION_STATE = "state"
_PARTITION_CODE = "code"
_PARTITION_TOKEN = "token"
_PARTITION_REFRESH = "refresh"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _is_expired(expires_at_str: str) -> bool:
    return expires_at_str < _iso(_utcnow())


class OAuthStore:
    """OAuth フロー用のデータストア（Azure Table Storage）。"""

    def __init__(self, connection_string: str, table_name: str = "oauthdata") -> None:
        service = TableServiceClient.from_connection_string(connection_string)
        self._client = service.get_table_client(table_name)
        try:
            self._client.create_table()
        except Exception:
            pass  # 既存テーブルは無視

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _get(self, pk: str, rk: str) -> dict[str, Any] | None:
        try:
            return dict(self._client.get_entity(partition_key=pk, row_key=rk))
        except ResourceNotFoundError:
            return None

    def _put(self, entity: dict[str, Any]) -> None:
        self._client.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)

    def _delete(self, pk: str, rk: str) -> None:
        try:
            self._client.delete_entity(partition_key=pk, row_key=rk)
        except ResourceNotFoundError:
            pass

    # ------------------------------------------------------------------
    # クライアント管理（DCR）
    # ------------------------------------------------------------------

    def save_client(
        self,
        client_id: str,
        client_secret: str,
        redirect_uris: list[str],
        client_name: str,
    ) -> None:
        """DCR で登録されたクライアントを保存する。"""
        self._put(
            {
                "PartitionKey": _PARTITION_CLIENT,
                "RowKey": client_id,
                "client_secret": client_secret,
                "redirect_uris": json.dumps(redirect_uris, ensure_ascii=False),
                "client_name": client_name,
                "created_at": _iso(_utcnow()),
            }
        )
        logger.info("OAuth client registered: %s (%s)", client_id, client_name)

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        """クライアント情報を返す。存在しない場合は None。"""
        e = self._get(_PARTITION_CLIENT, client_id)
        if e is None:
            return None
        return {
            "client_id": e["RowKey"],
            "client_secret": e.get("client_secret", ""),
            "redirect_uris": json.loads(e.get("redirect_uris", "[]")),
            "client_name": e.get("client_name", ""),
        }

    # ------------------------------------------------------------------
    # state 管理（Google OAuth フロー用）
    # ------------------------------------------------------------------

    def save_state(
        self,
        google_state: str,
        *,
        original_state: str,
        redirect_uri: str,
        client_id: str,
        code_challenge: str,
        code_challenge_method: str,
        ttl_seconds: int = 600,
    ) -> None:
        """Google に渡す state と、Claude から受け取ったパラメータを紐付けて保存する。"""
        expires_at = _iso(_utcnow() + timedelta(seconds=ttl_seconds))
        self._put(
            {
                "PartitionKey": _PARTITION_STATE,
                "RowKey": google_state,
                "original_state": original_state,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "expires_at": expires_at,
            }
        )

    def pop_state(self, google_state: str) -> dict[str, Any] | None:
        """state を取得して削除する（ワンタイム）。期限切れの場合は None を返す。"""
        e = self._get(_PARTITION_STATE, google_state)
        if e is None:
            return None
        self._delete(_PARTITION_STATE, google_state)
        if _is_expired(e["expires_at"]):
            logger.warning("OAuth state expired: %s", google_state)
            return None
        return {
            "original_state": e["original_state"],
            "redirect_uri": e["redirect_uri"],
            "client_id": e["client_id"],
            "code_challenge": e["code_challenge"],
            "code_challenge_method": e["code_challenge_method"],
        }

    # ------------------------------------------------------------------
    # 認可コード管理
    # ------------------------------------------------------------------

    def save_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        user_sub: str,
        user_email: str,
        ttl_seconds: int = 300,
    ) -> None:
        """認可コードを保存する（有効期限: 5 分）。"""
        expires_at = _iso(_utcnow() + timedelta(seconds=ttl_seconds))
        self._put(
            {
                "PartitionKey": _PARTITION_CODE,
                "RowKey": code,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "user_sub": user_sub,
                "user_email": user_email,
                "expires_at": expires_at,
            }
        )

    def pop_code(self, code: str) -> dict[str, Any] | None:
        """認可コードを取得して削除する（ワンタイム）。期限切れの場合は None を返す。"""
        e = self._get(_PARTITION_CODE, code)
        if e is None:
            return None
        self._delete(_PARTITION_CODE, code)
        if _is_expired(e["expires_at"]):
            logger.warning("OAuth auth code expired: %s", code[:8])
            return None
        return {
            "client_id": e["client_id"],
            "redirect_uri": e["redirect_uri"],
            "code_challenge": e["code_challenge"],
            "code_challenge_method": e["code_challenge_method"],
            "user_sub": e["user_sub"],
            "user_email": e["user_email"],
        }

    # ------------------------------------------------------------------
    # アクセストークン・リフレッシュトークン管理
    # ------------------------------------------------------------------

    def save_access_token(
        self,
        access_token: str,
        *,
        refresh_token: str,
        client_id: str,
        user_sub: str,
        user_email: str,
        access_ttl_seconds: int = 3600,
        refresh_ttl_seconds: int = 2_592_000,  # 30 日
    ) -> None:
        """アクセストークンとリフレッシュトークンを発行・保存する。"""
        now = _utcnow()
        # アクセストークン本体
        self._put(
            {
                "PartitionKey": _PARTITION_TOKEN,
                "RowKey": access_token,
                "refresh_token": refresh_token,
                "client_id": client_id,
                "user_sub": user_sub,
                "user_email": user_email,
                "access_expires_at": _iso(now + timedelta(seconds=access_ttl_seconds)),
                "refresh_expires_at": _iso(now + timedelta(seconds=refresh_ttl_seconds)),
                "revoked": "0",
            }
        )
        # リフレッシュトークン（逆引き用）
        self._put(
            {
                "PartitionKey": _PARTITION_REFRESH,
                "RowKey": refresh_token,
                "access_token": access_token,
                "client_id": client_id,
                "user_sub": user_sub,
                "user_email": user_email,
                "expires_at": _iso(now + timedelta(seconds=refresh_ttl_seconds)),
                "revoked": "0",
            }
        )
        logger.info(
            "OAuth tokens issued: client=%s user=%s access_expires=%s",
            client_id,
            user_email,
            _iso(now + timedelta(seconds=access_ttl_seconds)),
        )

    def verify_access_token(self, access_token: str) -> dict[str, Any] | None:
        """有効なアクセストークンであれば user_sub/user_email/client_id を返す。無効なら None。"""
        e = self._get(_PARTITION_TOKEN, access_token)
        if e is None:
            return None
        if e.get("revoked") == "1":
            return None
        if _is_expired(e["access_expires_at"]):
            return None
        return {
            "user_sub": e["user_sub"],
            "user_email": e["user_email"],
            "client_id": e["client_id"],
        }

    def get_refresh_token_info(self, refresh_token: str) -> dict[str, Any] | None:
        """有効なリフレッシュトークンの情報を返す。無効なら None。"""
        e = self._get(_PARTITION_REFRESH, refresh_token)
        if e is None:
            return None
        if e.get("revoked") == "1":
            return None
        if _is_expired(e["expires_at"]):
            return None
        return {
            "access_token": e["access_token"],
            "client_id": e["client_id"],
            "user_sub": e["user_sub"],
            "user_email": e["user_email"],
        }

    def revoke_token(self, access_token: str) -> None:
        """アクセストークンを無効化する。"""
        e = self._get(_PARTITION_TOKEN, access_token)
        if e:
            e["revoked"] = "1"
            self._put(e)
            # 対応するリフレッシュトークンも無効化
            rt = e.get("refresh_token", "")
            if rt:
                re = self._get(_PARTITION_REFRESH, rt)
                if re:
                    re["revoked"] = "1"
                    self._put(re)
            logger.info("OAuth token revoked: %s...", access_token[:8])

    def rotate_tokens(
        self,
        old_refresh_token: str,
        new_access_token: str,
        new_refresh_token: str,
        client_id: str,
        user_sub: str,
        user_email: str,
        access_ttl_seconds: int = 3600,
        refresh_ttl_seconds: int = 2_592_000,
    ) -> None:
        """リフレッシュ時に古いトークンを無効化して新しいトークンを発行する。"""
        # 古いリフレッシュトークンから古いアクセストークンを探して無効化
        old_info = self._get(_PARTITION_REFRESH, old_refresh_token)
        if old_info:
            old_access = old_info.get("access_token", "")
            if old_access:
                self.revoke_token(old_access)
            # リフレッシュトークンも無効化
            old_info["revoked"] = "1"
            self._put(old_info)
        # 新しいトークンを発行
        self.save_access_token(
            new_access_token,
            refresh_token=new_refresh_token,
            client_id=client_id,
            user_sub=user_sub,
            user_email=user_email,
            access_ttl_seconds=access_ttl_seconds,
            refresh_ttl_seconds=refresh_ttl_seconds,
        )
