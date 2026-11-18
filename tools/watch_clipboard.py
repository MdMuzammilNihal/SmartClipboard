"""Manual check: watch the real clipboard and print lengths only.

Run it, copy text from a browser, an editor, and other apps, and confirm a
line appears for each copy. The copied text itself is never printed, logged,
or saved: only its length and Windows' clipboard sequence number are shown.

    .\\.venv\\Scripts\\python.exe tools\\watch_clipboard.py

WARNING: the privacy gate is active (it honours the Windows opt-out formats
that some password managers set), but the app exclusion list is empty and
the gate has not been checked against a real password manager yet. This
tool keeps each copied text in memory for a moment and then discards it,
but do not copy real passwords while it runs.
"""

import logging
import queue
import sys

from smartclip import logsafe
from smartclip.listener import ClipboardEvent, ClipboardListener


def main() -> None:
    # Show the listener's own log lines, which hold lengths and counters only.
    logsafe.attach_handler(logging.StreamHandler(sys.stdout))

    events: "queue.Queue[ClipboardEvent]" = queue.Queue()
    listener = ClipboardListener(events)
    listener.start()
    print("Watching the clipboard. Copy some text in any app. Press Ctrl+C to stop.")
    print("Only lengths and sequence numbers are shown, never the text.")
    try:
        while True:
            try:
                # Take each event off the queue and drop it straight away.
                events.get(timeout=0.5)
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()
