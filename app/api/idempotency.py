from __future__ import annotations

import hashlib
import json

from app.domain.schemas import AnalysisRequest


def hash_idempotency_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scoped_idempotency_key_hash(client_id: str, value: str) -> str:
    """Hash an idempotency key inside a client namespace.

    The raw key and client_id are never persisted; only this digest is stored.
    Identical Idempotency-Key strings used by different clients therefore do not
    conflict in the shared ledger.
    """
    return hashlib.sha256(f"{client_id}\x00{value}".encode("utf-8")).hexdigest()


def research_request_hash(request: AnalysisRequest) -> str:
    payload = request.model_dump(mode="json")
    # Idempotency identifies the semantic operation, not a caller-selected report ID.
    payload.pop("request_id", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_idempotency_key(value: str) -> str:
    key = value.strip()
    if len(key) < 8 or len(key) > 200:
        raise ValueError("Idempotency-Key must be between 8 and 200 characters")
    return key
