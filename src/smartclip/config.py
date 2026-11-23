"""Where the app keeps its files.

Everything lives under %LOCALAPPDATA%\\SmartClipboard\\, never inside the
repository. Local (not roaming) app-data is used because DPAPI-protected data
is tied to this Windows account on this PC and is unreadable anywhere else.
"""

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

APP_DIR_NAME = "SmartClipboard"
DATABASE_FILE_NAME = "items.db"
LOG_DIR_NAME = "logs"
DEDUPE_KEY_FILE_NAME = "dedupe.key"
LABELED_LOG_FILE_NAME = "labeled.db"
# Window layout choices (search bar and filter chips shown or hidden). Plain
# JSON with two true/false values; never any clipboard content.
WINDOW_PREFS_FILE_NAME = "window.json"
# Settings chosen in the settings panel (settings.py): numbers, true/false
# values, a font choice and the excluded app names; never clipboard content.
SETTINGS_FILE_NAME = "settings.json"


@dataclass(frozen=True)
class SecretThresholds:
    """Where the secret score (0 to 1) is cut into three outcomes.

    Below `unsure` an item is normal. From `unsure` upward it gets the
    "Review?" badge. At `high` or above it counts as clearly secret, but only
    when it also came from a login-type app (see LOGIN_SOURCE_APPS).

    These are starting values, not measured ones. They lean low on purpose:
    missing a real secret is worse than a false alarm.
    """

    unsure: float = 0.45
    high: float = 0.7


SECRET_THRESHOLDS = SecretThresholds()

# Process names (lower case, for example "fakemanager.exe") treated as
# login-type sources. Empty until the owner chooses which apps belong here.
LOGIN_SOURCE_APPS: frozenset[str] = frozenset()

# Apps whose copies are never captured, by executable name (compared in lower
# case, for example "fakemanager.exe"). Empty by the owner's decision until
# the owner adds apps; real process names must be checked on the owner's
# machine first. The settings screen will edit this later.
EXCLUDED_APPS: frozenset[str] = frozenset()

# How long secrets and unsure items are kept (owner's decision: 2 hours),
# counted from the first copy. Copying one again does not restart it
# (owner's decision, 2026-11-18).
SECRET_RETENTION = timedelta(hours=2)

# How long normal items are kept after they were last copied (owner's
# decision: 90 days, a starting value to adjust after daily use). Pinned
# normal items are never deleted by retention.
NORMAL_RETENTION = timedelta(days=90)

# How long a secret or unsure item stays on the clipboard after copy-back
# before it is cleared (CLAUDE.md section 9: "about 30 seconds").
COPYBACK_CLEAR_AFTER = timedelta(seconds=30)

# How long a secret copied in another app stays on the clipboard after the
# app captures it, before it is cleared (owner's decision 2026-11-19,
# option B). Only clearly secret items, and only if nothing was copied since.
CAPTURED_SECRET_CLEAR_AFTER = timedelta(seconds=30)

# Remove confirmed secrets from Windows clipboard history (Win+V) as well
# (owner's decision 2026-11-19, option C; see winhistory.py). An item still
# on the clipboard is removed after this delay: deleting its Win+V entry
# also empties the clipboard, so it waits for the paste window plus a small
# margin, letting the 30-second clipboard clear run first. An older item
# marked secret is removed at once.
REMOVE_SECRETS_FROM_WIN_HISTORY = True
WIN_HISTORY_REMOVE_AFTER = CAPTURED_SECRET_CLEAR_AFTER + timedelta(seconds=2)

# The private labeled-data log for Phase 2 (task 20; see datalog.py). Off by
# default (owner's decision). Set to True to opt in; the settings screen will
# switch it later. Secret and unsure items are only written redacted.
LABELED_LOG_ENABLED = False

# The shortest time between two VACUUM runs. A starting value, not measured:
# VACUUM rewrites the whole file, so it should not run after every cleanup.
VACUUM_INTERVAL = timedelta(days=1)

# How long a secret or unsure item stays readable after the owner clicks to
# reveal it in the window (owner's decision, 2026-11-18). It is masked again
# sooner if the window hides.
REVEAL_DURATION = timedelta(seconds=10)

# Choosing an item in the window pastes it into the app that was in front
# when the window opened, as Win+V does (owner's decision 2026-11-19; see
# ui/paste.py). False: choosing only copies it, and you press Ctrl+V.
PASTE_ON_CHOOSE = True

# Keep the search window out of screenshots, recordings, and screen sharing
# (see ui/capture_guard.py). Set to False to take screenshots of it.
EXCLUDE_FROM_SCREEN_CAPTURE = True

# The name of the lock that stops a second copy of the app from running. It
# lives in this Windows session only (the "Local" namespace), so other
# accounts' copies are not affected.
SINGLE_INSTANCE_NAME = "Local\\SmartClipboard-single-instance"

# How often the app checks for expired items while it runs. A starting value:
# a secret can outlive its 2-hour deadline by up to this long.
CLEANUP_INTERVAL = timedelta(minutes=1)

# Global hotkeys (owner's decisions, 2026-11-12 and 2026-11-18). Not yet
# tested for clashes with other apps.
HOTKEY_OPEN_WINDOW = "Ctrl+Alt+V"
HOTKEY_PAUSE = "Ctrl+Alt+P"
HOTKEY_MARK_SECRET = "Ctrl+Alt+S"
HOTKEY_FORCE_SAVE = "Ctrl+Alt+F"


def app_data_dir() -> Path:
    """Return the app's data folder. Does not create it."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA is not set; this app runs on Windows only")
    return Path(base) / APP_DIR_NAME


def database_path() -> Path:
    """Return the path of the encrypted item database."""
    return app_data_dir() / DATABASE_FILE_NAME


def dedupe_key_path() -> Path:
    """Return the path of the DPAPI-protected key used to detect duplicates."""
    return app_data_dir() / DEDUPE_KEY_FILE_NAME


def log_dir() -> Path:
    """Return the folder for log files."""
    return app_data_dir() / LOG_DIR_NAME


def labeled_log_path() -> Path:
    """Return the path of the opt-in labeled-data log (task 20)."""
    return app_data_dir() / LABELED_LOG_FILE_NAME


def window_prefs_path() -> Path:
    """Return the path of the window layout choices (ui/prefs.py)."""
    return app_data_dir() / WINDOW_PREFS_FILE_NAME


def settings_path() -> Path:
    """Return the path of the in-app settings file (settings.py)."""
    return app_data_dir() / SETTINGS_FILE_NAME
