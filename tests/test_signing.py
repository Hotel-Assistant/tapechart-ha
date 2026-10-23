"""The shared signing vectors (contract §3 + §9 H5/H6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.tapechart.signing import (
    compute_signature,
    parse_signature_header,
    signature_base,
    signature_header,
    verify_signature,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "vectors.json").read_text(encoding="utf-8")
)
CASES = VECTORS["cases"]
SECRET = VECTORS["secret"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_vectors(case: dict) -> None:
    """Base and HMAC match the shared vectors byte for byte."""
    body = case["body"].encode("utf-8")
    base = signature_base(case["t"], case["id"], case["method"], case["path"], body)
    assert base == case["base"].encode("utf-8")
    assert (
        compute_signature(
            SECRET, case["t"], case["id"], case["method"], case["path"], body
        )
        == case["v1"]
    )
    header = signature_header(
        SECRET, case["t"], case["id"], case["method"], case["path"], body
    )
    assert header == f"t={case['t']},v1={case['v1']}"
    assert verify_signature(
        SECRET, header, case["id"], case["method"], case["path"], body, case["t"] + 299
    )


def test_verify_refuses() -> None:
    """Stale, tampered, wrong id, wrong path or malformed headers fail."""
    case = CASES[2]
    body = case["body"].encode()
    header = f"t={case['t']},v1={case['v1']}"
    args = (case["id"], case["method"], case["path"], body)
    assert not verify_signature(SECRET, header, *args, case["t"] + 301)
    assert not verify_signature(SECRET, header, *args, case["t"] - 301)
    assert not verify_signature(SECRET, header, "other-id", *args[1:], case["t"])
    assert not verify_signature(
        SECRET, header, case["id"], "POST", "/api/webhook/abc", body, case["t"]
    )
    assert not verify_signature(
        SECRET, header, case["id"], "POST", case["path"], body + b" ", case["t"]
    )
    assert not verify_signature("0" * 64, header, *args, case["t"])
    assert not verify_signature(SECRET, None, *args, case["t"])
    assert not verify_signature(SECRET, "garbage", *args, case["t"])
    assert not verify_signature(SECRET, header, "", *args[1:], case["t"])


def test_parse_header() -> None:
    """Several v1 values are allowed (rotation); t must be an integer."""
    assert parse_signature_header("t=5,v1=AB,v1=cd") == (5, ["ab", "cd"])
    assert parse_signature_header("t=x,v1=ab") is None
    assert parse_signature_header("v1=ab") is None
    assert parse_signature_header("t=5") is None
