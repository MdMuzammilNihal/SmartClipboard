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
- Group headers (PINNED, TODAY, ...) are fixed words and never hold item
  text. A header's group comes from the time of the last copy only.
- Nothing here logs, prints, or stores content.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo

from smartclip import config, naming, retention
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
    # Pink, not purple: purple is the accent colour (owner's decision 2026-11-24).
    Label.CODE: Badge("Code", "#d6336c"),
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

@dataclass(frozen=True)
class TagFilter:
    """A filter chip for one tag (UI plan item 9). Tags are compared ignoring case."""

    name: str

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TagFilter) and naming.tag_key(self.name) == naming.tag_key(other.name)

    def __hash__(self) -> int:
        return hash(naming.tag_key(self.name))


# A badge filter is a content type, a secret state, or a tag.
Filter = Label | SecretState | TagFilter


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
    # When the item was last copied (UTC). Decides its day group; never shown.
    copied_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    # Time left before retention deletes a secret or Review? item; None for normal items.
    remaining: timedelta | None = None
    # How the card body is drawn for a normal item (see Preview); None for masked
    # or revealed secrets, which always show as plain text or bullets.
    rich: "Preview | None" = None
    # The owner's name and tags for a pinned item (naming.py); never on secrets.
    title: str | None = None
    tags: tuple[str, ...] = ()
    can_name: bool = False  # pinned and not secret: the card menu offers Rename and Add tag


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
    if isinstance(active, TagFilter) and not any(
        naming.tag_key(tag) == naming.tag_key(active.name) for tag in payload.tags
    ):
        return False
    words = query.casefold().split()
    if not words:
        return True
    if masked:
        # Never matched by anything while masked (see SECURITY REVIEW above).
        return False
    # Titles and tags count as text, but only reach here for a normal item
    # (secrets cannot have any, and a masked item returned above).
    named = " ".join([payload.title or "", *payload.tags])
    haystack = f"{payload.content}\n{payload.source_app or ''}\n{named}".casefold()
    return all(word in haystack for word in words)


def build_rows(
    items: Iterable[StoredItem],
    query: str,
    active: Filter | None,
    reveals: RevealState,
    now: datetime,
    policy: retention.RetentionPolicy | None = None,
    expanded: frozenset[int] = frozenset(),
) -> list[Row]:
    """The rows to show, in the order given (History.items() is newest first).

    Search words are matched case-insensitively against the text and the
    source app; every word must appear.
    """
    rows: list[Row] = []
    policy = policy or retention.RetentionPolicy()
    for item in items:
        payload = item.payload
        sensitive = retention.is_sensitive(payload)
        remaining: timedelta | None = None
        if sensitive:
            deadline = retention.deadline(item, policy)
            remaining = deadline - now if deadline is not None else None
            if remaining is not None and remaining <= timedelta(0):
                continue  # expired; hidden now, deleted at the next cleanup
        masked = is_masked(item, reveals, now)
        if not matches(item, query, active, masked):
            continue
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
                copied_at=retention.last_copied(item),
                remaining=remaining,
                rich=None if sensitive else preview_for(payload.label, payload.content, item.id in expanded),
                title=payload.title,
                tags=payload.tags,
                can_name=payload.pinned and not sensitive,
            )
        )
    return rows


def filter_name(active: Filter) -> str:
    """The text shown for the active badge filter."""
    if isinstance(active, TagFilter):
        return f"#{active.name}"
    if isinstance(active, Label):
        return LABEL_BADGES[active].text
    return STATE_BADGES[active].text


# --- Day groups (UI plan item 2) ---

GROUP_PINNED = "PINNED"
GROUP_TODAY = "TODAY"
GROUP_YESTERDAY = "YESTERDAY"
GROUP_THIS_WEEK = "THIS WEEK"
GROUP_OLDER = "OLDER"
GROUP_RESULTS = "RESULTS"
_GROUP_ORDER = (GROUP_PINNED, GROUP_TODAY, GROUP_YESTERDAY, GROUP_THIS_WEEK, GROUP_OLDER)


@dataclass(frozen=True)
class Header:
    """A label that starts a group of cards. Not an item: it cannot be selected."""

    text: str


def day_group(copied_at: datetime, today: date, tz: tzinfo | None = None) -> str:
    """The day group for a copy time: compares local calendar dates, not 24-hour spans.

    TODAY and YESTERDAY are the local dates; THIS WEEK is the 2 to 6 days
    before today; OLDER is anything further back. A time in the future (a
    clock set back) counts as today. `tz` is for tests; None means this
    PC's local time zone.
    """
    days_ago = (today - copied_at.astimezone(tz).date()).days
    if days_ago <= 0:
        return GROUP_TODAY
    if days_ago == 1:
        return GROUP_YESTERDAY
    if days_ago < 7:
        return GROUP_THIS_WEEK
    return GROUP_OLDER


def arrange(
    rows: list[Row], now: datetime, searching: bool, tz: tzinfo | None = None
) -> list[Row | Header]:
    """Put the rows under headers. Pinned first, then by day; flat while searching.

    The rows must already be newest first. Empty groups get no header. While
    searching there is one RESULTS header and the order is left alone.
    """
    if not rows:
        return []
    if searching:
        return [Header(GROUP_RESULTS), *rows]
    today = now.astimezone(tz).date()
    groups: dict[str, list[Row]] = {name: [] for name in _GROUP_ORDER}
    for row in rows:
        name = GROUP_PINNED if row.pinned else day_group(row.copied_at, today, tz)
        groups[name].append(row)
    arranged: list[Row | Header] = []
    for name in _GROUP_ORDER:
        if groups[name]:
            arranged.append(Header(name))
            arranged.extend(groups[name])
    return arranged


# --- Quick paste (UI plan item 3) ---

QUICK_PASTE_COUNT = 9


# --- The secret countdown (UI plan item 7) ---

COUNTDOWN_WARNING = timedelta(minutes=15)  # the pill turns amber below this
COUNTDOWN_DANGER = timedelta(minutes=2)  # and red below this


def countdown_text(remaining: timedelta) -> str:
    """"1h 42m left", "42m left" or "under 1 min left". Time only, never content."""
    seconds = max(0, int(remaining.total_seconds()))
    if seconds < 60:
        return "under 1 min left"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes}m left" if hours else f"{minutes}m left"


def countdown_level(remaining: timedelta) -> str:
    """"danger" under 2 minutes, "warning" under 15, otherwise "normal"."""
    if remaining < COUNTDOWN_DANGER:
        return "danger"
    if remaining < COUNTDOWN_WARNING:
        return "warning"
    return "normal"


# --- Rich previews by type (UI plan item 8) ---

MAX_PREVIEW_INPUT = 4000  # text handed to any pattern or splitter; keeps patterns fast
JSON_PARSE_LIMIT = 20000  # bigger JSON stays plain text
COLLAPSED_LINES = 4  # code, shell and SQL, and error text, before "Show more"
EXPANDED_LINES = 12  # after "Show more"
JSON_KEYS_SHOWN = 8
LONG_TEXT_CHARS = 600  # how much of a long note "Show more" reveals
_LINK = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)?(?P<host>[^/\s?#]+)(?P<rest>\S*)")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")
_CODE_LABELS = (Label.CODE, Label.SHELL_COMMAND, Label.SQL)


@dataclass(frozen=True)
class Seg:
    """A piece of one preview line: text, how it looks, and where to cut it if too long."""

    text: str
    style: str = "normal"  # "normal", "bold", "muted" or "mono"
    elide: str = "right"  # "right", or "middle" for the long part of a link or path


@dataclass(frozen=True)
class Preview:
    """What the body of a normal card shows. Pure data: the window draws it.

    `lines` are drawn one per row and cut with "..." to the card width,
    except when `wrap` is set: then the single paragraph is wrapped to the
    card width. `expandable` means a "Show more" toggle is offered;
    `expanded` says it is open.
    """

    kind: str  # "plain", "link", "json", "code", "path", "error" or "colour"
    lines: tuple[tuple[Seg, ...], ...]
    wrap: bool = False
    swatch: str | None = None  # "#rrggbb" for a colour swatch before the text
    chip: str | None = None  # file extension shown as a small chip (paths)
    expandable: bool = False
    expanded: bool = False


def preview_for(label: Label, content: str, expanded: bool = False) -> Preview:
    """The preview for a normal (non-secret) item. Never called for masked secrets.

    Falls back to plain wrapped text whenever the text does not fit its
    type's shape (for example a link label on text with spaces).
    """
    text = content[:MAX_PREVIEW_INPUT]
    stripped = text.strip()
    if _COLOUR.fullmatch(stripped):
        return Preview("colour", ((Seg(stripped),),), swatch=stripped.lower())
    made: Preview | None = None
    if label is Label.LINK:
        made = _link_preview(stripped)
    elif label is Label.JSON:
        made = _json_preview(content, expanded)
    elif label in _CODE_LABELS:
        made = _code_preview(text, expanded)
    elif label is Label.FILE_PATH:
        made = _path_preview(stripped)
    elif label is Label.ERROR_MESSAGE:
        made = _error_preview(text, expanded)
    return made or _plain_preview(content, expanded)


def _plain_preview(content: str, expanded: bool) -> Preview:
    collapsed = " ".join(content[: MAX_PREVIEW_INPUT].split())
    long = len(" ".join(content.split())) > PREVIEW_CHARS
    if expanded and long:
        shown = collapsed if len(collapsed) <= LONG_TEXT_CHARS else collapsed[: LONG_TEXT_CHARS - 1] + "\u2026"
    else:
        shown = preview_text(content)
    return Preview("plain", ((Seg(shown),),), wrap=True, expandable=long, expanded=expanded and long)


def _link_preview(text: str) -> Preview | None:
    if not text or any(ch.isspace() for ch in text):
        return None
    found = _LINK.fullmatch(text)
    if found is None:
        return None
    host, rest = found.group("host"), found.group("rest")
    return Preview("link", ((Seg(host, "bold"), Seg(rest, "muted", "middle")),))


def _json_preview(content: str, expanded: bool) -> Preview | None:
    if len(content) > JSON_PARSE_LIMIT:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data:
        return None
    if expanded:
        pretty = json.dumps(data, indent=2, ensure_ascii=False).splitlines()
        shown = pretty[:EXPANDED_LINES]
        if len(pretty) > EXPANDED_LINES:
            shown[-1] = "\u2026"
        return Preview(
            "json", tuple((Seg(line, "mono"),) for line in shown), expandable=True, expanded=True
        )
    keys = [str(key) for key in list(data)[:JSON_KEYS_SHOWN]]
    more = ", \u2026" if len(data) > JSON_KEYS_SHOWN else ""
    summary = "{ " + ", ".join(keys) + more + " }"
    return Preview("json", ((Seg(summary),),), expandable=True)


def _code_preview(text: str, expanded: bool) -> Preview | None:
    lines = [line.expandtabs(4).rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    limit = EXPANDED_LINES if expanded else COLLAPSED_LINES
    shown = lines[:limit]
    if len(lines) > limit:
        shown[-1] = shown[-1] + " \u2026"
    return Preview(
        "code",
        tuple((Seg(line, "mono"),) for line in shown),
        expandable=len(lines) > COLLAPSED_LINES,
        expanded=expanded and len(lines) > COLLAPSED_LINES,
    )


def _path_preview(text: str) -> Preview | None:
    if not text or "\n" in text:
        return None
    cut = max(text.rfind("\\"), text.rfind("/"))
    folder, name = text[: cut + 1], text[cut + 1 :]
    if not name:
        return None
    extension = name.rpartition(".")[2].lower() if "." in name.strip(".") else ""
    chip = extension if extension.isalnum() and len(extension) <= 6 else None
    segments = (Seg(folder, "muted", "middle"), Seg(name, "bold")) if folder else (Seg(name, "bold"),)
    return Preview("path", (segments,), chip=chip)


def _error_preview(text: str, expanded: bool) -> Preview | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    limit = EXPANDED_LINES if expanded else COLLAPSED_LINES - 1  # the bold line plus two
    rest = lines[1:limit]
    shown = ((Seg(lines[0], "bold"),), *((Seg(line, "muted"),) for line in rest))
    more = len(lines) > COLLAPSED_LINES - 1
    return Preview("error", shown, expandable=more, expanded=expanded and more)
