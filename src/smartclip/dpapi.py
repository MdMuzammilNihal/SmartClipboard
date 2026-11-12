"""Thin wrapper around Windows DPAPI (CryptProtectData / CryptUnprotectData).

SECURITY REVIEW (owner): this module is the only place encryption happens.
No custom cryptography is used; Windows does all of it.

Choices made here, and why:

- No description string is passed to CryptProtectData. Windows stores the
  description unencrypted inside the blob, so anything put there would be
  readable by whoever holds the database file.
- No "optional entropy" (a second secret mixed into the key) is used. The only
  value we could supply is a constant in this public source code, which would
  add nothing against an attacker who can read the code.
- CRYPTPROTECT_UI_FORBIDDEN is set so Windows never shows a prompt; the call
  fails instead. A background app must not pop up dialogs.
- Errors are re-raised as DpapiError with a fixed message and no chained
  cause, so no data can travel in an exception message or traceback.

What this does not do: it does not protect against malware or anyone running
as the same Windows user, because DPAPI will decrypt for any process running
under that account.
"""

import pywintypes
import win32crypt

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiError(Exception):
    """DPAPI could not protect or unprotect data. Never carries the data."""


def protect(plaintext: bytes) -> bytes:
    """Encrypt bytes so only this Windows account on this PC can read them."""
    try:
        return win32crypt.CryptProtectData(
            plaintext, None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except pywintypes.error:
        raise DpapiError("CryptProtectData failed") from None


def unprotect(blob: bytes) -> bytes:
    """Decrypt a blob made by protect(). Raises DpapiError if it cannot."""
    try:
        _description, plaintext = win32crypt.CryptUnprotectData(
            blob, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except pywintypes.error:
        raise DpapiError("CryptUnprotectData failed") from None
    return plaintext
