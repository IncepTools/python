"""Constant-time HMAC webhook signing and verification.

Two failures this prevents:

* ``signature == expected`` on a string compares byte by byte and returns early
  on the first mismatch. The timing difference is small but measurable across
  enough requests, and it leaks the signature one byte at a time.
  :func:`verify` uses :func:`hmac.compare_digest`.
* **No timestamp check.** A signature stays valid forever, so a captured
  request can be replayed a year later. Pass ``timestamp`` and
  ``tolerance`` and a stale request is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from inceptools.core import errs

__all__ = ["PREFIX", "sign", "signed_payload", "verify"]

PREFIX = "sha256="


def signed_payload(payload: bytes, timestamp: int | None = None) -> bytes:
    """What is actually signed.

    When a timestamp is used it is prefixed into the signed bytes, so moving
    the timestamp invalidates the signature. Signing the body alone and
    sending the timestamp beside it lets an attacker change the timestamp
    freely, which defeats the replay window.
    """
    if timestamp is None:
        return payload
    return f"{timestamp}.".encode() + payload


def sign(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """``"sha256=<hex>"`` for *payload*."""
    digest = hmac.new(
        secret.encode("utf-8"), signed_payload(payload, timestamp), hashlib.sha256
    ).hexdigest()
    return PREFIX + digest


def verify(
    payload: bytes,
    secret: str,
    signature: str,
    *,
    timestamp: int | None = None,
    tolerance: float = 300.0,
) -> None:
    """Raise unless *signature* is valid for *payload*.

    Raises a 401 with code ``INVALID_SIGNATURE``, or ``STALE_SIGNATURE`` when
    the timestamp is outside *tolerance* seconds.
    """
    candidate = (signature or "").strip()
    if not candidate.startswith(PREFIX):
        raise errs.unauthorized("the signature is missing its algorithm prefix").with_code(
            "INVALID_SIGNATURE"
        )

    if timestamp is not None and tolerance > 0:
        drift = abs(time.time() - timestamp)
        if drift > tolerance:
            raise errs.unauthorized(f"the signature timestamp is {int(drift)}s old").with_code(
                "STALE_SIGNATURE"
            )

    expected = sign(payload, secret, timestamp=timestamp)
    if not hmac.compare_digest(expected, candidate):
        raise errs.unauthorized("the signature does not match").with_code("INVALID_SIGNATURE")
