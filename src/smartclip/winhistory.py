"""Remove confirmed secrets from Windows clipboard history (Win+V), option C.

When a copy turns out to be a clear secret, Windows has usually already
recorded it in clipboard history (if history is turned on). This module
finds the matching history entries and deletes them through the Windows
Runtime clipboard API (Clipboard.GetHistoryItemsAsync and
Clipboard.DeleteItemFromHistory), reached from Python with the pywinrt
packages (owner's approval, 2026-11-19).

Measured on the owner's machine (Windows 11, 2026-11-19, fake text only):

- A normal desktop process, also from a worker thread and in the
  background, can list history and delete entries.
- Deleting the entry of the item that is on the clipboard right now ALSO
  empties the clipboard. So app.py waits for the 30-second paste window
  before removing the current item's entry.
- Deleting an older entry leaves the current clipboard item alone.

SECURITY REVIEW (owner):

- Matching uses the same keyed hash as duplicate merging (dedupe.py): each
  history entry's text is hashed with the app's key and compared with the
  item's stored hash. The secret's own text is never handed to this module.
- Reading history means every text entry in Win+V passes through this
  process's memory while it is hashed. Nothing is logged except counts.
- All matching entries are deleted, including entries pinned in Win+V.
- If clipboard history is off, nothing is read or deleted.
- Not verified: whether deleting locally also removes an entry that cloud
  clipboard sync has already sent to other devices.
- Runs on its own single worker thread, so the window never waits for it.
"""

import asyncio
import hmac
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

from smartclip import logsafe

_log = logsafe.get_logger("winhistory")


class HistoryApi(Protocol):
    """The Win+V history operations needed here. Tests supply a fake."""

    def is_enabled(self) -> bool:
        """True if Windows clipboard history is turned on."""

    def text_entries(self) -> list[tuple[object, str]]:
        """Every history entry that holds text, as (entry, text)."""

    def delete(self, entry: object) -> bool:
        """Delete one entry. Returns True if Windows removed it."""


class WinRtHistoryApi:
    """The real Win+V history, through pywinrt."""

    def __init__(self) -> None:
        # Imported here so the rest of the app (and its tests) do not need
        # the Windows Runtime packages loaded.
        from winrt.windows.applicationmodel.datatransfer import (
            Clipboard,
            ClipboardHistoryItemsResultStatus,
            StandardDataFormats,
        )

        self._clipboard = Clipboard
        self._success = ClipboardHistoryItemsResultStatus.SUCCESS
        self._text_format = StandardDataFormats.text

    def is_enabled(self) -> bool:
        return bool(self._clipboard.is_history_enabled())

    def text_entries(self) -> list[tuple[object, str]]:
        return asyncio.run(self._text_entries())

    async def _text_entries(self) -> list[tuple[object, str]]:
        result = await self._clipboard.get_history_items_async()
        if result.status != self._success or result.items is None:
            logsafe.log_event(_log, "win_history_list_refused", status=int(result.status))
            return []
        entries: list[tuple[object, str]] = []
        for item in result.items:
            content = item.content
            if content.contains(self._text_format):
                entries.append((item, await content.get_text_async()))
        return entries

    def delete(self, entry: object) -> bool:
        return bool(self._clipboard.delete_item_from_history(entry))


def remove_matching(api: HistoryApi, digest_of: Callable[[str], str], digest: str) -> int:
    """Delete every history entry whose keyed hash equals `digest`. Returns how many."""
    if not api.is_enabled():
        return 0
    removed = 0
    for entry, text in api.text_entries():
        if hmac.compare_digest(digest_of(text), digest) and api.delete(entry):
            removed += 1
    return removed


class HistoryCleaner:
    """Runs removals on one worker thread. Safe to call submit() from any thread."""

    def __init__(self, digest_of: Callable[[str], str], api: HistoryApi | None = None) -> None:
        self._digest_of = digest_of
        self._api = api
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smartclip-winhistory")

    def submit(self, digest: str) -> "Future[int]":
        """Queue the removal of entries matching `digest`. The future gives the count."""
        return self._executor.submit(self._run, digest)

    def shutdown(self) -> None:
        """Finish queued removals, then stop the worker thread."""
        self._executor.shutdown(wait=True)

    def _run(self, digest: str) -> int:
        try:
            if self._api is None:
                self._api = WinRtHistoryApi()
            removed = remove_matching(self._api, self._digest_of, digest)
        except Exception as error:  # noqa: BLE001 - never let a cleanup crash the app
            logsafe.log_error(_log, "win_history_remove_failed", error)
            return 0
        logsafe.log_event(_log, "win_history_removed", entries=removed)
        return removed
