"""Where the app keeps its files.

Everything lives under %LOCALAPPDATA%\\SmartClipboard\\, never inside the
repository. Local (not roaming) app-data is used because DPAPI-protected data
is tied to this Windows account on this PC and is unreadable anywhere else.
"""

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "SmartClipboard"
DATABASE_FILE_NAME = "items.db"
LOG_DIR_NAME = "logs"


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


def app_data_dir() -> Path:
    """Return the app's data folder. Does not create it."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA is not set; this app runs on Windows only")
    return Path(base) / APP_DIR_NAME


def database_path() -> Path:
    """Return the path of the encrypted item database."""
    return app_data_dir() / DATABASE_FILE_NAME


def log_dir() -> Path:
    """Return the folder for log files."""
    return app_data_dir() / LOG_DIR_NAME
