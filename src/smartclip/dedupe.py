"""Keyed hash for spotting duplicate clipboard items.

SECURITY REVIEW (owner): how duplicates are detected, and why this way.

- Two items are duplicates when their text is exactly equal. The keyed hash
  is HMAC-SHA256 over the UTF-8 text, using Python's standard `hmac` and
  `hashlib` modules. No custom cryptography.
- Why keyed and not a plain SHA-256: a plain hash of a short secret can be
  brute-forced by anyone who sees it (hash every likely password and compare).
  With HMAC, guessing also needs the key.
- The key is 32 random bytes from the standard `secrets` module, protected
  with DPAPI (dpapi.py) and kept in its own file under
  %LOCALAPPDATA%\\SmartClipboard\\ (config.dedupe_key_path()), never in the
  repository.
- Where the hash lives: inside each item's encrypted payload (`dedupe_key`
  in storage.ItemPayload), never in a plaintext column. So the database file
  shows no hash of the content at all. The keyed hash is still used because
  it also stays safe if a decrypted payload is ever exposed, or if a later
  change moves it into a plaintext column to speed up lookups.
- An unreadable key file raises DedupeKeyError. It is never silently
  replaced: a new key would not match the hashes already stored, so every
  later copy would be saved twice without anyone noticing. Since the key and
  the database are protected by the same Windows account, a key that DPAPI
  cannot open usually means the database cannot be opened either (confirmed
  by the owner, 2026-11-18). What the app does then is decided with app.py;
  the suggestion is a clear error, no capture, and later a "reset key"
  option in settings.
- The key stays in Python memory while the app runs (see storage.py on
  memory limits).
"""

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Self

from smartclip import dpapi, logsafe

KEY_LENGTH = 32

_log = logsafe.get_logger("dedupe")


class DedupeKeyError(Exception):
    """The key file exists but cannot be used. Never carries the key."""


class DedupeKey:
    """The secret key behind the duplicate hash."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LENGTH:
            raise ValueError("dedupe key has the wrong length")
        self._key = key

    def __repr__(self) -> str:
        # The key must never end up in a log or a traceback by accident.
        return "DedupeKey(<hidden>)"

    @classmethod
    def load_or_create(cls, path: Path) -> Self:
        """Read the key from `path`, or make and save a new one if none exists."""
        if path.exists():
            return cls(_read_key(path))
        key = secrets.token_bytes(KEY_LENGTH)
        _write_key(path, key)
        logsafe.log_event(_log, "dedupe_key_created")
        return cls(key)

    def digest(self, content: str) -> str:
        """Return the keyed hash of the text, as 64 hex characters."""
        return hmac.new(self._key, content.encode("utf-8"), hashlib.sha256).hexdigest()


def _read_key(path: Path) -> bytes:
    try:
        key = dpapi.unprotect(path.read_bytes())
    except (OSError, dpapi.DpapiError):
        key = None
    if key is None or len(key) != KEY_LENGTH:
        logsafe.log_event(_log, "dedupe_key_unreadable")
        raise DedupeKeyError("dedupe key file cannot be read")
    return key


def _write_key(path: Path, key: bytes) -> None:
    """Write the protected key through a temporary file, so a crash leaves no half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(dpapi.protect(key))
    os.replace(temporary, path)
