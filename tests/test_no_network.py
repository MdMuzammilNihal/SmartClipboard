"""No-network checks (task 13).

1. Static: fails if any module in the app package imports a networking module.
2. Runtime: runs the app's flows with fake data in a fresh Python process
   (tests/no_network_probe.py) with an audit hook installed, and fails if
   Python opens a socket to anything other than this machine's loopback
   address, looks up a host name, or uses urllib, http.client, or similar.
   It also fails if a networking module other than the expected ones ends
   up loaded (see EXPECTED_LOADED).

SECURITY REVIEW (owner): what the runtime check found (2026-11-19):

- Loopback sockets: every asyncio.run() on Windows makes a connected pair of
  TCP sockets on 127.0.0.1 inside the process, for its own wake-up signal
  (Python's socket.socketpair). winhistory.py calls asyncio.run() to await
  the WinRT history calls, so each Win+V removal opens and closes one such
  pair. This traffic never leaves the machine. The test allows loopback
  addresses only, so a real outbound connection still fails it.
- Loaded but unused: socket and selectors are imported by the standard
  library's logging.handlers (used by logsafe.py); asyncio and ssl by
  asyncio itself (imported by winhistory.py and by the pywinrt packages).
  No HTTP, URL, or Qt networking module is loaded.

Limits, stated honestly:
- The static check reads import statements in our own source files only.
  It does not catch dynamic imports (importlib, __import__ with a computed
  name).
- The audit hook only sees Python-level calls. Network use by C or C++
  code that bypasses Python's socket module (Qt, WinRT, pywin32, or Windows
  itself, for example DPAPI talking to a domain controller on a work PC) is
  invisible to it. The manual check in the README covers the running app.
- The runtime check runs the flows with fake data and fakes for the
  clipboard writes; it does not press hotkeys or start main().
- Together they are a guard against accidents, not proof that no network
  call can happen.
"""

import ast
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


# --- Runtime check ---

TESTS_DIR = Path(__file__).parent
SRC_DIR = PACKAGE_DIR.parent
PROBE = TESTS_DIR / "no_network_probe.py"

# Networking modules that are loaded but not used by the app (see docstring).
EXPECTED_LOADED = frozenset({"socket", "_socket", "ssl", "_ssl", "select", "selectors", "asyncio"})
# The only audit events allowed: making a socket, and binding or connecting
# it to a loopback address (asyncio's internal socket pair).
ALLOWED_EVENTS = frozenset({"socket.__new__", "socket.bind", "socket.connect"})


def is_loopback(address_repr: str) -> bool:
    """True if a printed socket address such as "('127.0.0.1', 0)" is loopback."""
    try:
        host = ast.literal_eval(address_repr)[0]
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, SyntaxError, TypeError, IndexError):
        return False


def run_python(*args: str) -> str:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC_DIR), str(TESTS_DIR)]))
    done = subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, env=env, timeout=120
    )
    assert done.returncode == 0, f"probe failed with exit code {done.returncode}"
    return done.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def probe_result() -> dict[str, list]:
    pytest.importorskip("PySide6")
    pytest.importorskip("winrt.windows.applicationmodel.datatransfer")
    return json.loads(run_python(str(PROBE)))


def test_app_flows_use_no_network(probe_result: dict[str, list]) -> None:
    unexpected = [e for e in probe_result["events"] if e[0] not in ALLOWED_EVENTS]
    assert not unexpected, f"Network-related calls made: {unexpected}"
    outside = [
        e for e in probe_result["events"]
        if e[0] in {"socket.bind", "socket.connect"} and not is_loopback(e[1])
    ]
    assert not outside, f"Sockets bound or connected outside loopback: {outside}"


def test_no_unexpected_networking_modules_are_loaded(probe_result: dict[str, list]) -> None:
    unexpected = sorted(set(probe_result["modules"]) - EXPECTED_LOADED)
    assert not unexpected, f"Networking modules loaded: {unexpected}"


def test_probe_hook_sees_socket_use() -> None:
    """The hook must catch a real socket call, or the checks above prove nothing."""
    code = "; ".join(
        [
            "import json, no_network_probe as p, socket",
            "s = socket.socket()",
            "s.bind(('127.0.0.1', 0))",
            "s.close()",
            "socket.getaddrinfo('localhost', None)",
            "print(json.dumps(p._events))",
        ]
    )
    names = [event for event, _detail in json.loads(run_python("-c", code))]
    assert "socket.__new__" in names and "socket.bind" in names
    assert "socket.getaddrinfo" in names


def test_loopback_check() -> None:
    assert is_loopback("('127.0.0.1', 0)")
    assert is_loopback("('::1', 53000, 0, 0)")
    assert not is_loopback("('93.184.215.14', 443)")
    assert not is_loopback("('example.test', 443)")
    assert not is_loopback("garbage")
