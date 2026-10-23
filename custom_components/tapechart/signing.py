"""Request signing for the Tape Chart wire (contract §3).

`X-TapeChart-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, base)>` where
`base = t + "." + id + "." + METHOD + "." + PATH + "." + raw_body` (§9 H5: id is the
X-TapeChart-Id header; H6: PATH is the fixed logical path of the endpoint, never the
observed URL path). The HMAC key is the secret string's UTF-8 bytes.

Pure module: no Home Assistant imports, so it can move to a PyPI library later.
"""

from __future__ import annotations

import hashlib
import hmac

# Logical paths (§9 H6).
PATH_WEBHOOK = "/tapechart/webhook"


def signature_base(
    t: int, message_id: str, method: str, path: str, body: bytes
) -> bytes:
    """Return the exact bytes that are signed."""
    return f"{t}.{message_id}.{method.upper()}.{path}.".encode() + body


def compute_signature(
    secret: str, t: int, message_id: str, method: str, path: str, body: bytes
) -> str:
    """Return the hex HMAC-SHA256 of the signature base."""
    return hmac.new(
        secret.encode("utf-8"),
        signature_base(t, message_id, method, path, body),
        hashlib.sha256,
    ).hexdigest()


def signature_header(
    secret: str, t: int, message_id: str, method: str, path: str, body: bytes
) -> str:
    """Return the value of the X-TapeChart-Signature header."""
    return f"t={t},v1={compute_signature(secret, t, message_id, method, path, body)}"


def parse_signature_header(value: str | None) -> tuple[int, list[str]] | None:
    """Parse `t=..,v1=..` into (t, [v1, ...]); None when malformed."""
    if not value:
        return None
    t: int | None = None
    sigs: list[str] = []
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        if not sep:
            return None
        if key == "t":
            try:
                t = int(val)
            except ValueError:
                return None
        elif key == "v1":
            sigs.append(val.strip().lower())
    if t is None or not sigs:
        return None
    return t, sigs


def verify_signature(
    secret: str,
    header: str | None,
    message_id: str,
    method: str,
    path: str,
    body: bytes,
    now: float,
    tolerance: int = 300,
) -> bool:
    """Verify a signature header; constant-time compare, |now - t| <= tolerance."""
    parsed = parse_signature_header(header)
    if parsed is None or not message_id:
        return False
    t, sigs = parsed
    if abs(now - t) > tolerance:
        return False
    expected = compute_signature(secret, t, message_id, method, path, body)
    ok = False
    for sig in sigs:
        # Evaluate every candidate; never short-circuit on a match.
        ok |= hmac.compare_digest(expected, sig)
    return ok
