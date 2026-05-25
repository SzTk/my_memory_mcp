"""OAuth 2.1 + PKCE + Google OIDC ヘルパー。

提供する機能:
  - Google JWKS のキャッシュ付き取得（同期）
  - Google ID トークンの検証（PyJWT + RSAAlgorithm）
  - PKCE S256 チャレンジ検証
  - Google 認可 URL の構築
  - Google 認可コード ↔ ID トークン 交換（同期）
  - 乱数トークン生成
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any

import requests

# PyJWT + cryptography (RS256 のために cryptography が必要)
import jwt as pyjwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

# Google OIDC エンドポイント
_GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_ISSUER = "https://accounts.google.com"

# JWKS キャッシュ
_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 3600.0  # 1 時間


# ------------------------------------------------------------------
# Google JWKS / ID トークン検証
# ------------------------------------------------------------------


def _fetch_jwks() -> dict[str, Any]:
    global _jwks_cache, _jwks_fetched_at
    resp = requests.get(_GOOGLE_JWKS_URI, timeout=10)
    resp.raise_for_status()
    _jwks_cache = resp.json()
    _jwks_fetched_at = time.monotonic()
    return _jwks_cache


def _get_jwks() -> dict[str, Any]:
    if _jwks_cache and time.monotonic() - _jwks_fetched_at < _JWKS_TTL:
        return _jwks_cache
    return _fetch_jwks()


def decode_google_id_token(id_token: str, google_client_id: str) -> dict[str, Any]:
    """Google ID トークンを検証してクレームを返す。

    検証内容:
      - 署名（JWKS の公開鍵 + RS256）
      - audience（= google_client_id）
      - issuer（= https://accounts.google.com）
      - 有効期限

    Raises:
        ValueError: kid が見つからない場合
        jwt.exceptions.InvalidTokenError: 署名・期限・audience が不正な場合
    """
    global _jwks_fetched_at

    header = pyjwt.get_unverified_header(id_token)
    kid = header.get("kid")

    def _find_key(jwks: dict[str, Any]) -> dict[str, Any] | None:
        for k in jwks.get("keys", []):
            if k.get("kid") == kid:
                return k
        return None

    jwks = _get_jwks()
    key_data = _find_key(jwks)

    if key_data is None:
        # キーが見つからなければ JWKS をリフレッシュしてリトライ
        logger.info("Google JWKS cache miss for kid=%s, refreshing...", kid)
        _jwks_fetched_at = 0.0
        jwks = _fetch_jwks()
        key_data = _find_key(jwks)

    if key_data is None:
        raise ValueError(f"Google JWKS key not found: kid={kid}")

    public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
    claims: dict[str, Any] = pyjwt.decode(
        id_token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience=google_client_id,
        issuer=_GOOGLE_ISSUER,
    )
    return claims


# ------------------------------------------------------------------
# PKCE S256
# ------------------------------------------------------------------


def verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """PKCE S256 の検証: SHA256(code_verifier) を base64url エンコードして比較する。"""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


# ------------------------------------------------------------------
# トークン生成
# ------------------------------------------------------------------


def generate_token(n_bytes: int = 32) -> str:
    """URL セーフなランダムトークンを生成する（デフォルト 32 バイト）。"""
    return secrets.token_urlsafe(n_bytes)


# ------------------------------------------------------------------
# Google OAuth フロー
# ------------------------------------------------------------------


def build_google_auth_url(
    google_client_id: str,
    google_redirect_uri: str,
    state: str,
    scope: str = "openid email profile",
) -> str:
    """Google の認可 URL を構築して返す。"""
    params = {
        "client_id": google_client_id,
        "redirect_uri": google_redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "access_type": "offline",  # リフレッシュトークン取得用
        "prompt": "select_account",  # 毎回アカウント選択を表示
    }
    return f"{_GOOGLE_AUTH_URI}?{urllib.parse.urlencode(params)}"


def exchange_google_code(
    code: str,
    google_client_id: str,
    google_client_secret: str,
    google_redirect_uri: str,
) -> dict[str, Any]:
    """Google 認可コード → トークンレスポンスに交換する。

    Returns:
        Google のトークンレスポンス dict（id_token, access_token 等を含む）

    Raises:
        requests.HTTPError: Google がエラーを返した場合
    """
    payload = {
        "code": code,
        "client_id": google_client_id,
        "client_secret": google_client_secret,
        "redirect_uri": google_redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = requests.post(_GOOGLE_TOKEN_URI, data=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()  # type: ignore[return-value]


def get_google_user_info(
    google_token_response: dict[str, Any],
    google_client_id: str,
) -> tuple[str, str]:
    """Google トークンレスポンスからユーザーの sub と email を取得する。

    Returns:
        (sub, email) のタプル

    Raises:
        ValueError: id_token がない、email_verified でない場合
        jwt.exceptions.InvalidTokenError: ID トークンが不正な場合
    """
    id_token = google_token_response.get("id_token")
    if not id_token:
        raise ValueError("Google token response missing id_token")

    claims = decode_google_id_token(id_token, google_client_id)

    if not claims.get("email_verified", False):
        raise ValueError("Google account email not verified")

    sub: str = claims["sub"]
    email: str = claims.get("email", "")
    return sub, email
