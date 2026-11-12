"""Tests for the DPAPI wrapper. All values are fake.

These call the real Windows DPAPI, so they only run on Windows.
"""

import pytest

from smartclip import dpapi

FAKE_PLAINTEXT = b"FAKE-plaintext-for-dpapi-test-0001"


def test_round_trip_returns_the_original_bytes() -> None:
    assert dpapi.unprotect(dpapi.protect(FAKE_PLAINTEXT)) == FAKE_PLAINTEXT


def test_blob_does_not_contain_the_plaintext() -> None:
    blob = dpapi.protect(FAKE_PLAINTEXT)
    assert blob != FAKE_PLAINTEXT
    assert FAKE_PLAINTEXT not in blob


def test_garbage_fails_cleanly() -> None:
    with pytest.raises(dpapi.DpapiError):
        dpapi.unprotect(b"this is not a DPAPI blob")


def test_tampered_blob_fails_cleanly() -> None:
    blob = bytearray(dpapi.protect(FAKE_PLAINTEXT))
    blob[len(blob) // 2] ^= 0xFF
    with pytest.raises(dpapi.DpapiError):
        dpapi.unprotect(bytes(blob))


def test_error_carries_no_data_and_no_chained_cause() -> None:
    with pytest.raises(dpapi.DpapiError) as caught:
        dpapi.unprotect(FAKE_PLAINTEXT)
    assert "FAKE" not in str(caught.value)
    assert caught.value.__cause__ is None
