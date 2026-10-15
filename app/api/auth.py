from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.settings import Settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_ID_RE = re.compile(r"^req_([0-9a-f]{32})_([0-9a-f]{32})$")
_LOCAL_CLIENT_ID = "local-development"


@dataclass(frozen=True)
class AuthPrincipal:
    client_id: str
    key_hash: str


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _validate_key_hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("API key hashes must be SHA-256 hex digests")
    return normalized


def _derived_client_id(key_hash: str) -> str:
    digest = hashlib.sha256(f"client:{key_hash}".encode("utf-8")).hexdigest()
    return f"legacy_{digest[:24]}"


def configured_api_principals(settings: Settings) -> tuple[AuthPrincipal, ...]:
    """Parse API-key configuration into stable client-scoped principals.

    Explicit PUBLIC_API_CLIENT_KEYS entries take the form `client_id=sha256` and
    support multiple keys for the same client. Legacy PUBLIC_API_KEY_HASHES remain
    supported and map each key to a deterministic client_id.
    """

    principals_by_hash: dict[str, AuthPrincipal] = {}
    explicit = settings.public_api_client_keys or ""
    for raw_entry in explicit.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError("PUBLIC_API_CLIENT_KEYS must contain client_id=sha256 entries")
        client_id, raw_hash = (part.strip() for part in entry.split("=", 1))
        if not _CLIENT_ID_RE.fullmatch(client_id):
            raise ValueError(
                "PUBLIC_API_CLIENT_KEYS client_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            )
        if client_id == _LOCAL_CLIENT_ID:
            raise ValueError("PUBLIC_API_CLIENT_KEYS cannot use the reserved local-development client_id")
        key_hash = _validate_key_hash(raw_hash)
        existing = principals_by_hash.get(key_hash)
        if existing is not None and existing.client_id != client_id:
            raise ValueError("The same API key hash cannot belong to multiple client IDs")
        principals_by_hash[key_hash] = AuthPrincipal(client_id=client_id, key_hash=key_hash)

    legacy = settings.public_api_key_hashes or ""
    for raw_hash in legacy.split(","):
        if not raw_hash.strip():
            continue
        key_hash = _validate_key_hash(raw_hash)
        if key_hash not in principals_by_hash:
            principals_by_hash[key_hash] = AuthPrincipal(
                client_id=_derived_client_id(key_hash),
                key_hash=key_hash,
            )

    return tuple(principals_by_hash.values())


def configured_api_key_hashes(settings: Settings) -> tuple[str, ...]:
    return tuple(principal.key_hash for principal in configured_api_principals(settings))


def authenticate_api_key(
    settings: Settings,
    api_key: str | None,
    *,
    public_contract: bool = False,
) -> AuthPrincipal:
    principals = configured_api_principals(settings)
    if not principals:
        return AuthPrincipal(client_id=_LOCAL_CLIENT_ID, key_hash="auth-disabled")

    if not api_key:
        code = "UNAUTHORIZED" if public_contract else "missing_api_key"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": code, "message": "X-API-Key is required"},
        )

    candidate = hash_api_key(api_key)
    for principal in principals:
        if hmac.compare_digest(candidate, principal.key_hash):
            return principal

    code = "UNAUTHORIZED" if public_contract else "invalid_api_key"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": "API key is invalid"},
    )


def client_request_namespace(client_id: str) -> str:
    return hashlib.sha256(f"request-owner:{client_id}".encode("utf-8")).hexdigest()[:32]


def new_client_request_id(client_id: str) -> str:
    return f"req_{client_request_namespace(client_id)}_{uuid.uuid4().hex}"


def request_id_belongs_to_client(request_id: str, client_id: str) -> bool:
    if client_id == _LOCAL_CLIENT_ID:
        return True
    match = _REQUEST_ID_RE.fullmatch(request_id)
    return bool(match and hmac.compare_digest(match.group(1), client_request_namespace(client_id)))


def build_api_key_dependency(settings: Settings) -> Callable:
    async def require_api_key(api_key: str | None = Security(api_key_header)) -> AuthPrincipal:
        return authenticate_api_key(settings, api_key)

    return require_api_key
