"""Constant-time verification, and a replay window."""

from __future__ import annotations

import time

import pytest

from inceptools.core import errs
from inceptools.web import webhook

BODY = b'{"event":"order.placed"}'
SECRET = "whsec_abc"


def test_a_valid_signature_verifies():
    webhook.verify(BODY, SECRET, webhook.sign(BODY, SECRET))


def test_a_tampered_body_is_refused():
    signature = webhook.sign(BODY, SECRET)
    with pytest.raises(errs.AppError) as caught:
        webhook.verify(b'{"event":"order.refunded"}', SECRET, signature)
    assert caught.value.code == "INVALID_SIGNATURE"
    assert caught.value.status_code == 401


def test_a_wrong_secret_is_refused():
    with pytest.raises(errs.AppError, match="does not match"):
        webhook.verify(BODY, "other", webhook.sign(BODY, SECRET))


@pytest.mark.parametrize("signature", ["", "deadbeef", "md5=abc", "sha256=nothex"])
def test_a_malformed_signature_is_refused(signature):
    with pytest.raises(errs.AppError):
        webhook.verify(BODY, SECRET, signature)


def test_a_stale_signature_is_refused():
    old = int(time.time()) - 900
    with pytest.raises(errs.AppError) as caught:
        webhook.verify(BODY, SECRET, webhook.sign(BODY, SECRET, timestamp=old), timestamp=old)
    assert caught.value.code == "STALE_SIGNATURE"


def test_a_recent_signature_is_accepted():
    now = int(time.time())
    webhook.verify(BODY, SECRET, webhook.sign(BODY, SECRET, timestamp=now), timestamp=now)


def test_the_timestamp_is_part_of_what_is_signed():
    """Otherwise an attacker can move the timestamp and defeat the window."""
    now = int(time.time())
    body_only = webhook.sign(BODY, SECRET)
    with pytest.raises(errs.AppError):
        webhook.verify(BODY, SECRET, body_only, timestamp=now)
