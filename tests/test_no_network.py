"""No-network check (CLAUDE.md section 13, task 13).

Fails if any module in the app package imports a networking module.

Limits, stated honestly:
- This is a static check of import statements in our own source files. It
  does not catch dynamic imports (importlib, __import__ with a computed name)
  and it does not inspect what third-party dependencies do internally.
- It is a guard against accidents, not proof that no network call can happen.
"""

import ast
from pathlib import Path

import smartclip

PACKAGE_DIR = Path(smartclip.__file__).parent

# Top-level modules that open network connections.
BANNED_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "socketserver",
        "webbrowser",
    }
)

# Qt submodules that can reach the network.
BANNED_QT_SUBMODULES = frozenset(
    {
        "QtNetwork",
        "QtNetworkAuth",
        "QtWebEngineCore",
        "QtWebEngineWidgets",
        "QtWebSockets",
        "QtWebChannel",
    }
)


def find_banned_imports(source: str) -> list[str]:
    """Return the banned module names imported by the given source code."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            # "from PySide6 import QtNetwork" must be caught as PySide6.QtNetwork.
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] in BANNED_MODULES:
                found.append(name)
            elif parts[0] == "PySide6" and len(parts) > 1 and parts[1] in BANNED_QT_SUBMODULES:
                found.append(name)
    return found


def test_package_imports_cleanly() -> None:
    assert smartclip.__version__


def test_app_code_imports_no_networking_modules() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        banned = find_banned_imports(path.read_text(encoding="utf-8"))
        if banned:
            offenders[str(path.relative_to(PACKAGE_DIR))] = banned
    assert not offenders, f"Networking imports found in app code: {offenders}"


def test_checker_catches_plain_import() -> None:
    assert find_banned_imports("import socket") == ["socket"]


def test_checker_catches_from_import() -> None:
    assert find_banned_imports("from urllib.request import urlopen") != []


def test_checker_catches_qt_network() -> None:
    assert find_banned_imports("from PySide6 import QtNetwork") == ["PySide6.QtNetwork"]
    assert find_banned_imports("import PySide6.QtNetwork") == ["PySide6.QtNetwork"]


def test_checker_allows_safe_imports() -> None:
    assert find_banned_imports("import sqlite3\nimport hmac\nfrom PySide6 import QtWidgets") == []
