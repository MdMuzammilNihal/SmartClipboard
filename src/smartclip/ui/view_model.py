"""What the search window shows, as plain Python (tasks 9 and 10).

The window (window.py) only draws what this module returns, so search,
filtering, and masking can be tested without Qt.

Each item becomes one Row: a coloured type badge, a state badge for secret
("Secret") and unsure ("Review?") items, a one-line preview, the source app,
and the age.

SECURITY REVIEW (owner): what the window can show, and what search can see.

- Secret and unsure items are masked: their preview is a fixed run of
  bullets (MASK). The bullets are always the same length, so the window
  does not show how long the secret is.
- A masked item is never matched by search. While any search text is typed,
  masked items are left out of the results entirely; their label and source
  app are not matched either (owner's decision, 2026-11-18), so typing
  cannot be used to probe what a masked item holds. With an empty search, or
  with only a badge filter, masked items are listed, still masked.
- Clicking reveals one item for `config.REVEAL_DURATION` (10 seconds, owner's
  decision 2026-11-18). While revealed it shows its preview and is searched
  like a normal item. RevealState masks it again when the time is up, and
  the window calls hide_all() whenever it hides, so nothing stays revealed
  behind a closed window.
- The label badge and the source app of a masked item are shown. They are
  needed to tell items apart and are already visible to anyone who can open
  the window. The source app is a best guess (see sourceapp.py).
- Previews are cut to one line of PREVIEW_CHARS characters. The full text of
  a normal item is not put in tooltips or anywhere else in the window.
- Nothing here logs, prints, or stores content.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from smartclip import config, retention
from smartclip.labels import Label, SecretState
from smartclip.storage import StoredItem

MASK = "•" * 8
PREVIEW_CHARS = 120


@dataclass(frozen=True)
class Badge:
    """A coloured tag: the text shown and its background and text colours."""

    text: str
    background: str
    foreground: str = "#ffffff"


# Colours chosen by Claude (owner: "you pick", 2026-11-18). Secret is red and
# unsure is amber; the type colours only need to be easy to tell apart.
LABEL_BADGES: dict[Label, Badge] = {
    Label.LINK: Badge("Link", "#1c7ed6"),
    Label.CODE: Badge("Code", "#7048e8"),
    Label.JSON: Badge("JSON", "#0c8599"),
    Label.SHELL_COMMAND: Badge("Shell", "#343a40"),
    Label.FILE_PATH: Badge("Path", "#9c6b30"),
    Label.ENV_VAR: Badge("Env", "#5c7cfa"),
    Label.ERROR_MESSAGE: Badge("Error", "#d9480f"),
    Label.SQL: Badge("SQL", "#2b8a3e"),
    Label.IP_ADDRESS: Badge("IP", "#3b5bdb"),
    Label.HASH: Badge("Hash", "#5f6b7a"),
    Label.ADDRESS: Badge("Address", "#66a80f"),
    Label.NOTE: Badge("Note", "#868e96"),
    Label.POSSIBLE_SECRET: Badge("Secret?", "#c92a2a"),
}

STATE_BADGES: dict[SecretState, Badge] = {
    SecretState.SECRET: Badge("Secret", "#c92a2a"),
    SecretState.UNSURE: Badge("Review?", "#f59f00", "#212529"),
}

# A badge filter is either a content type or a secret state.
Filter = Label | SecretState


@dataclass(frozen=True)
class Row:
    """One line in the window."""

    item_id: int
    label: Label
    label_badge: Badge
    state: SecretState
    state_badge: Badge | None
    preview: str
    source_app: str
    age: str
    masked: bool
    revealed: bool
    pinned: bool
    can_pin: bool


class RevealState:
    """Which masked items are revealed, and until when. Used on the UI thread."""

    def __init__(self, duration: timedelta = config.REVEAL_DURATION) -> None:
        self._duration = duration
        self._until: dict[int, datetime] = {}

    def reveal(self, item_id: int, now: datetime) -> None:
        """Reveal an item until `now` plus the reveal duration."""
        self._until[item_id] = now + self._duration

    def hide(self, item_id: int) -> None:
        """Mask one item again."""
        self._until.pop(item_id, None)

    def hide_all(self) -> None:
        """Mask everything again, for example when the window hides."""
        self._until.clear()

    def is_revealed(self, item_id: int, now: datetime) -> bool:
        """True while the item's reveal time has not run out."""
        until = self._until.get(item_id)
        return until is not None and now < until

    def expire(self, now: datetime) -> list[int]:
        """Forget reveals whose time is up. Returns the ids masked again."""
        done = [item_id for item_id, until in self._until.items() if now >= until]
        for item_id in done:
            del self._until[item_id]
        return done

    def any_revealed(self) -> bool:
        return bool(self._until)


def preview_text(content: str, limit: int = PREVIEW_CHARS) -> str:
    """One line: whitespace runs (including line breaks) become single spaces."""
    line = " ".join(content.split())
    if len(line) > limit:
        return line[: limit - 1] + "…"
    return line


def age_text(age: timedelta) -> str:
    """A short age such as "now", "5m", "3h", "2d"."""
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def is_masked(item: StoredItem, reveals: RevealState, now: datetime) -> bool:
    """True for a secret or unsure item that is not revealed right now."""
    return retention.is_sensitive(item.payload) and not reveals.is_revealed(item.id, now)


def matches(item: StoredItem, query: str, active: Filter | None, masked: bool) -> bool:
    """Whether an item belongs in the results for this search and badge filter."""
    payload = item.payload
    if isinstance(active, Label) and payload.label is not active:
        return False
    if isinstance(active, SecretState) and payload.secret_state is not active:
        return False
    words = query.casefold().split()
    if not words:
        return True
    if masked:
        # Never matched by anything while masked (see SECURITY REVIEW above).
        return False
    haystack = f"{payload.content}\n{payload.source_app or ''}".casefold()
    return all(word in haystack for word in words)


def build_rows(
    items: Iterable[StoredItem],
    query: str,
    active: Filter | None,
    reveals: RevealState,
    now: datetime,
) -> list[Row]:
    """The rows to show, in the order given (History.items() is newest first).

    Search words are matched case-insensitively against the text and the
    source app; every word must appear.
    """
    rows: list[Row] = []
    for item in items:
        masked = is_masked(item, reveals, now)
        if not matches(item, query, active, masked):
            continue
        payload = item.payload
        sensitive = retention.is_sensitive(payload)
        rows.append(
            Row(
                item_id=item.id,
                label=payload.label,
                label_badge=LABEL_BADGES[payload.label],
                state=payload.secret_state,
                state_badge=STATE_BADGES.get(payload.secret_state),
                preview=MASK if masked else preview_text(payload.content),
                source_app=payload.source_app or "",
                age=age_text(now - retention.last_copied(item)),
                masked=masked,
                revealed=sensitive and not masked,
                pinned=payload.pinned,
                can_pin=not sensitive,
            )
        )
    return rows


def filter_name(active: Filter) -> str:
    """The text shown for the active badge filter."""
    if isinstance(active, Label):
        return LABEL_BADGES[active].text
    return STATE_BADGES[active].text
