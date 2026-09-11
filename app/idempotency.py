import hashlib
import json

from app.errors import MISSING_IDEMPOTENCY_KEY, LedgerError

MAX_KEY_LENGTH = 255


def validate_key(key: str | None) -> str:
    if not key or len(key) > MAX_KEY_LENGTH:
        raise LedgerError(
            400,
            MISSING_IDEMPOTENCY_KEY,
            f"Idempotency-Key header is required, non-empty and at most "
            f"{MAX_KEY_LENGTH} characters.",
        )
    return key


def request_hash(endpoint_path: str, body: dict) -> str:
    """Fingerprint of a request, used to detect a key being reused for a different
    request. Keys are sorted so that two equal bodies always hash the same."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((endpoint_path + canonical).encode()).hexdigest()
