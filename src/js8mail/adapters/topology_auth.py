"""Authentication primitives for optional topology telemetry.

The bundled application token is intentionally not used as a secret. This
module signs only an opted-in, allowlisted request body with a per-install key.
"""

from __future__ import annotations

import hashlib
import hmac


def canonical_signature_input(
    method: str, path: str, timestamp: int, nonce: str, body: bytes
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode()


def sign_request(
    secret: bytes, method: str, path: str, timestamp: int, nonce: str, body: bytes
) -> str:
    payload = canonical_signature_input(method, path, timestamp, nonce, body)
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_request(
    secret: bytes,
    signature: str,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
    *,
    now: int,
    max_clock_skew: int = 300,
) -> bool:
    if abs(now - timestamp) > max_clock_skew:
        return False
    expected = sign_request(secret, method, path, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)
