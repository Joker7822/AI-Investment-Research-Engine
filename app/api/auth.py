from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.settings import Settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def configured_api_key_hashes(settings: Settings) -> tuple[str, ...]:
    raw = settings.public_api_key_hashes or ""
    hashes = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    for value in hashes:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("PUBLIC_API_KEY_HASHES must contain comma-separated SHA-256 hex digests")
    return hashes


def build_api_key_dependency(settings: Settings) -> Callable:
    allowed_hashes = configured_api_key_hashes(settings)

    async def require_api_key(api_key: str | None = Security(api_key_header)) -> str:
        # Local development remains usable when no key hashes are configured.
        if not allowed_hashes:
            return "auth-disabled"
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "missing_api_key", "message": "X-API-Key is required"},
            )
        candidate = hash_api_key(api_key)
        if not any(hmac.compare_digest(candidate, allowed) for allowed in allowed_hashes):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_api_key", "message": "API key is invalid"},
            )
        return candidate

    return require_api_key
