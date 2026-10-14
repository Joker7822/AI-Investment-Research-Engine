from __future__ import annotations

import hashlib
import json

from app.domain.schemas import AnalysisRequest


def hash_idempotency_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
