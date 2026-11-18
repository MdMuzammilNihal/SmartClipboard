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

# The shortest time between two VACUUM runs. A starting value, not measured:
# VACUUM rewrites the whole file, so it should not run after every cleanup.
VACUUM_INTERVAL = timedelta(days=1)

# Global hotkeys (owner's decisions, 2026-11-12 and 2026-11-18). Not yet
# tested for clashes with other apps. Opening the window (Ctrl+Alt+V) and the
# pause toggle (Ctrl+Alt+P) are registered when the UI exists.
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
