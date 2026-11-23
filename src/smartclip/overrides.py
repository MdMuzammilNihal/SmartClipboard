"""Manual overrides (task 11): mark the last item as secret.

Owner's decisions (2026-11-18):

- Ctrl+Alt+S always marks the last item as secret. It never toggles: a
  toggle would turn an item the detector already flagged back into a normal
  item kept for 90 days, which is the worse mistake. Pressing it again does
  nothing more.
- Marking an item normal is done in the window (the "Review?" badge), not by
  hotkey. The window calls mark_normal() below.
- Force-save (Ctrl+Alt+F) lives in the listener, because it must read the
  clipboard (see listener.ClipboardListener.request_force_save).

"Last item" means the item stored from what is on the clipboard right now.
LastCapture remembers the clipboard sequence number of the latest stored
item; if the clipboard has changed since (a newer copy the gate skipped, or
a copy not stored yet), the mark is refused rather than applied to an older
item the owner did not mean. This relies on our own read leaving the number
unchanged. Checked on the owner's machine (2026-11-18) with fake text for
plain text, text Windows converts from ANSI, and text an app supplies only
when asked (delayed rendering); the first two are opt-in tests in
tests/test_listener.py.

SECURITY REVIEW (owner):

- Marking secret also unpins the item (secrets cannot be pinned) and sets
  the short secret expiry, counted from the moment of marking.
- An item that is already secret is returned unchanged, so pressing the
  hotkey again never extends how long a secret is kept.
- mark_normal() (from the window) clears the secret state and the short
  expiry. The item then follows normal retention, counted from its last
  copy. An item labeled "possible secret" is labeled again by the content
  rules, since that label only came from secret detection. It does not pin
  the item; the owner pins it separately if wanted.
- Not wired yet: storage is not connected to the listener, so nothing calls
  this outside tests. The app (app.py, not built) will run it on the thread
  that owns the Store, since a Store must stay on its own thread.
"""

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from smartclip import classify, config
from smartclip.labels import Label, SecretState
from smartclip.storage import ItemPayload


def mark_secret(
    payload: ItemPayload,
    now: datetime,
    retention: timedelta = config.SECRET_RETENTION,
) -> ItemPayload:
    """Return the payload marked as secret, or unchanged if it already is.

    `retention` is how long the secret is kept: the length chosen in the
    settings panel, or the config default.
    """
    if payload.secret_state is SecretState.SECRET:
        return payload
    return replace(
        payload,
        secret_state=SecretState.SECRET,
        pinned=False,
        expires_at=now + retention,
    )


def mark_normal(payload: ItemPayload) -> ItemPayload:
    """Return the payload marked normal, or unchanged if it already is."""
    if payload.secret_state is SecretState.NORMAL:
        return payload
    label = payload.label
    if label is Label.POSSIBLE_SECRET:
        label = classify.classify(payload.content).label
    return replace(payload, secret_state=SecretState.NORMAL, label=label, expires_at=None)


@dataclass(frozen=True)
class _Capture:
    item_id: int
    sequence_number: int


class LastCapture:
    """The latest stored item and the clipboard change it came from. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: _Capture | None = None

    def record(self, item_id: int, sequence_number: int) -> None:
        """Remember the item just stored from a clipboard change."""
        with self._lock:
            self._latest = _Capture(item_id, sequence_number)

    def forget(self, item_id: int) -> None:
        """Drop the record if that item was deleted."""
        with self._lock:
            if self._latest is not None and self._latest.item_id == item_id:
                self._latest = None

    def item_for(self, current_sequence: int) -> int | None:
        """Return the latest item's id if it is what the clipboard holds now."""
        with self._lock:
            latest = self._latest
        if latest is None or latest.sequence_number != current_sequence:
            return None
        return latest.item_id
