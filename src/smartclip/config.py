"""Where the app keeps its files.

Everything lives under %LOCALAPPDATA%\\SmartClipboard\\, never inside the
repository. Local (not roaming) app-data is used because DPAPI-protected data
is tied to this Windows account on this PC and is unreadable anywhere else.
"""

import os
from pathlib import Path

APP_DIR_NAME = "SmartClipboard"
DATABASE_FILE_NAME = "items.db"
LOG_DIR_NAME = "logs"


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
