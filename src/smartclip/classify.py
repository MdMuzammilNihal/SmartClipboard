"""Rule-based content-type labels.

classify() gives a piece of text exactly one label. The rules run in a fixed
order, most specific first, and the first one that matches wins. Anything that
matches no rule is a Note.

These rules are starting points, not final. They are simple on purpose: each
one should be explainable in a sentence. They will mislabel some text, and
tests/test_classify.py lists known cases where they do. Phase 2 measures how
often.

What this module does not do: it does not decide whether text is a secret.
That is a separate step (secret detection), which can turn any label into
"possible secret". Until that step exists, a lone random-looking token such
as a password is labeled Note here.

SECURITY REVIEW (owner):
- This module never logs, prints, or stores anything. It only returns a label.
- For links it also returns the domain. Anything before an "@" in the address
  (a user name and password embedded in the URL) is dropped, and so are the
  port, path, and query.
- Only the first MAX_ANALYZED_CHARS characters are examined by the pattern
  rules, so a huge copy cannot make labeling slow.
"""

import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from smartclip.labels import Label

# Pattern rules look at the start of very long text only.
MAX_ANALYZED_CHARS = 20_000
# JSON has to be parsed whole. Above this size it is not attempted.
MAX_JSON_CHARS = 2_000_000


@dataclass(frozen=True)
class Classification:
    """The label for a piece of text, plus the domain when it is a link."""

    label: Label
    domain: str | None = None


# --- Link ---

_URL = re.compile(r"(?:https?|ftp)://[^\s/?#]+\S*", re.IGNORECASE)
_WWW = re.compile(r"www\.[^\s/?#.]+\.[^\s/?#]+\S*", re.IGNORECASE)
# Optional scheme, optional "user:password@", then the host we want.
_DOMAIN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://)?(?:[^/@\s?#]*@)?(\[[^\]\s]+\]|[^/:?#\s]+)", re.IGNORECASE
)


def _is_link(text: str) -> bool:
    """The whole text is one web address."""
    return _URL.fullmatch(text) is not None or _WWW.fullmatch(text) is not None


def _link_domain(text: str) -> str | None:
    """Return the host of a link, lowercased, without credentials or port."""
    match = _DOMAIN.match(text)
    return match.group(1).lower() if match else None


# --- JSON ---


def _is_json(text: str) -> bool:
    """The text parses as a JSON object or array."""
    if len(text) > MAX_JSON_CHARS or text[0] not in "{[" or text[-1] not in "}]":
        return False
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return False
    return isinstance(parsed, (dict, list))


# --- Error message ---

_ERROR = re.compile(
    "|".join(
        [
            r"Traceback \(most recent call last\)",
            # "ValueError: ..." or a bare "java.lang.NullPointerException" line.
            r"^\s*[\w.]*(?:Error|Exception)(?::|\s*$)",
            # "error: ...", "fatal: ...", "error[E0308]: ..."
            r"^\s*(?:error|fatal|panic)(?::|\[)",
            # A stack frame: "    at com.x.Y.z(Y.java:10)" or "at f (file.js:1:2)".
            r"^\s+at .+[(:]\S*:\d+\)?\s*$",
            r"^npm ERR!",
            r"^Exception in thread",
            r"Segmentation fault",
            r"Unhandled (?:exception|rejection)",
        ]
    ),
    re.IGNORECASE | re.MULTILINE,
)


# A log line that starts with an upper-case level: "[ERROR] ..." or "ERROR 2026-...".
# Case matters here, so this is separate from the case-insensitive pattern above.
_LOG_ERROR = re.compile(r"^\s*\[?(?:ERROR|FATAL|CRITICAL)\]?[\s:]", re.MULTILINE)


def _is_error_message(text: str) -> bool:
    """The text contains a line that looks like an error or a stack trace."""
    return _ERROR.search(text) is not None or _LOG_ERROR.search(text) is not None


# --- SQL ---

_SQL_OBJECT = r"(?:table|index|view|database|schema|function|trigger|procedure)"
_SQL = re.compile(
    r"\s*(?:"
    + "|".join(
        [
            r"select\b.+\bfrom\b",
            r"select\s+[^;]+;",
            r"insert\s+into\b",
            r"update\s+\S+\s+set\b",
            r"delete\s+from\b",
            r"(?:create|drop|alter)\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?"
            r"(?:unique\s+)?" + _SQL_OBJECT + r"\b",
            r"truncate\s+table\b",
            r"with\s+\w+\s+as\s*\(",
        ]
    )
    + ")",
    re.IGNORECASE | re.DOTALL,
)


def _is_sql(text: str) -> bool:
    """The text starts with a SQL statement, not just a SQL-sounding word."""
    return _SQL.match(text) is not None


# --- Env var ---

# NAME=value with an upper-case name of two or more characters and no space
# before "=". "MAX = 5" (with spaces) is left for the code rule, and "E=mc2"
# (one-letter name) is not treated as an environment variable.
_ENV_LINE = re.compile(
    r"(?:(?:export|set)\s+)?[A-Z_][A-Z0-9_]+=.*"
    r"|\$env:[A-Za-z_]\w*\s*=\s*\S.*"
)


def _is_env_var(text: str) -> bool:
    """Every line is NAME=value (blank lines and # comments are allowed)."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    return bool(lines) and all(_ENV_LINE.fullmatch(line) for line in lines)


# --- File path ---

_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/][^<>:\"|?*\r\n]*")
_WINDOWS_NETWORK = re.compile(r"\\\\[^\\/\s]+[\\/][^<>:\"|?*\r\n]*")
_PATH_SEGMENT = re.compile(r"[\w.\-@+~]+")
_EXTENSION = re.compile(r"\.\w{1,8}$")


def _is_file_path(text: str) -> bool:
    """The whole text is one file or folder path."""
    path = text.strip("\"'")
    if _WINDOWS_ABSOLUTE.fullmatch(path) or _WINDOWS_NETWORK.fullmatch(path):
        return True
    if "://" in path or any(character.isspace() for character in path):
        return False
    separators = path.count("/") + path.count("\\")
    if separators == 0:
        return False
    is_absolute = path[0] in "/~"
    segments = [segment for segment in re.split(r"[\\/]", path) if segment]
    if not segments or not all(_PATH_SEGMENT.fullmatch(segment) for segment in segments):
        return False
    # "12/05/2026" and "1/2" are not paths.
    if all(segment.isdigit() for segment in segments):
        return False
    # One separator is not enough on its own ("and/or", "km/h", "/help"):
    # require a deeper path or a file extension.
    has_extension = _EXTENSION.search(segments[-1]) is not None
    return has_extension or len(segments) >= (2 if is_absolute else 3)


# --- IP address ---

_IPV4_WITH_EXTRAS = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?(?:/\d{1,2})?")
_IPV6_BRACKETED = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::\d{1,5})?")
_IPV6_WITH_PREFIX = re.compile(r"([0-9A-Fa-f:.]+)(?:/\d{1,3})?")


def _is_ip_address(text: str) -> bool:
    """The whole text is one IPv4 or IPv6 address, optionally with port or prefix."""
    for pattern in (_IPV4_WITH_EXTRAS, _IPV6_BRACKETED, _IPV6_WITH_PREFIX):
        match = pattern.fullmatch(text)
        if match is None or len(match.group(1)) < 3:
            continue
        try:
            ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        return True
    return False


# --- Hash ---

_HASH = re.compile(r"(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})")


def _is_hash(text: str) -> bool:
    """The whole text is a hex string of 32, 40, or 64 characters."""
    return _HASH.fullmatch(text) is not None


# --- Shell command ---

_PROMPT = re.compile(r"(?:\$|>|#|PS [^>\n]*>)\s+")
# Words that are almost always a command when they start a line.
_SURE_COMMANDS = frozenset(
    """git npm npx pip pip3 docker docker-compose kubectl curl wget ssh scp sudo
    apt apt-get brew cargo yarn pnpm conda chmod chown mkdir grep uv winget choco
    scoop pytest terraform helm gh az aws gcloud systemctl rsync tar unzip""".split()
)
# Words that are also ordinary English, so they need a second sign of a command.
_MAYBE_COMMANDS = frozenset(
    """cd ls cat echo rm cp mv go make node python python3 py java export set dir
    find sort kill touch which code start open ping type del copy move sed awk
    head tail source dotnet ruby php bash sh powershell pwsh cmd""".split()
)
# A flag, a pipe or redirect, a path, a file name, a variable, or an assignment.
_COMMAND_SIGN = re.compile(r"(?:^|\s)-{1,2}[A-Za-z]|[|<>]|&&|\S[/\\]\S|\S\.\w{1,5}\b|\$\w|=")
_POWERSHELL_CMDLET = re.compile(
    r"(?:Get|Set|New|Remove|Start|Stop|Invoke|Import|Export|Test|Select|Where|ForEach"
    r"|Write|Read|Add|Clear|Copy|Move|Rename|Out|ConvertTo|ConvertFrom|Install"
    r"|Uninstall|Update|Enable|Disable)-[A-Z][A-Za-z]+"
)
# If the second word is one of these, the text is a sentence, not a command.
_SENTENCE_WORDS = frozenset(
    """is was are were has have had can will would should the a an and or for to of
    in on it you me we this that up sure""".split()
)
_MAX_COMMAND_LINES = 5


def _is_shell_command(text: str) -> bool:
    """The text starts with a known command and looks like it is being run."""
    lines = text.splitlines()
    if len(lines) > _MAX_COMMAND_LINES:
        return False
    first_line = _PROMPT.sub("", lines[0].strip(), count=1)
    words = first_line.split()
    if not words:
        return False
    command = words[0]
    if _POWERSHELL_CMDLET.fullmatch(command):
        return True
    if len(words) < 2 or words[1].lower() in _SENTENCE_WORDS:
        return False
    if command in _SURE_COMMANDS:
        return True
    arguments = first_line[len(command) :]
    return command in _MAYBE_COMMANDS and _COMMAND_SIGN.search(arguments) is not None


# --- Code ---

# Any one of these is enough.
_STRONG_CODE = re.compile(
    "|".join(
        [
            r"^\s*(?:async\s+)?def \w+\s*\(",
            r"^\s*class \w+\s*[:({]",
            r"^\s*import [\w.]+(?:\s+as\s+\w+)?\s*;?\s*$",
            r"^\s*from [\w.]+ import [\w*]",
            r"^\s*(?:export\s+)?(?:async\s+)?function\s*\w*\s*\(",
            r"^\s*(?:export\s+)?(?:const|let|var) \w+\s*=",
            r"^\s*#include\s*[<\"]",
            r"^#!/",
            r"<\?php",
            r"^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(",
            r"^\s*(?:fn|func) \w+\s*\(",
            r"^\s*<(\w+)[^>]*>.*</\1>\s*$",
            r"^\s*@\w+(?:\(.*\))?\s*$",
        ]
    ),
    re.MULTILINE,
)
# Two different ones of these are needed.
_WEAK_CODE = [
    re.compile(r"\w\([^()\n]*\)"),  # a call: name(...)
    re.compile(r";\s*$", re.MULTILINE),  # a line ending in a semicolon
    re.compile(r"[{}]\s*$", re.MULTILINE),  # a line ending in a brace
    re.compile(r"==|!=|=>|->|:=|&&|\|\||\+=|<="),  # operators
    re.compile(r"^\s*[\w.\[\]\"']+\s*=\s*\S", re.MULTILINE),  # an assignment
    re.compile(r"^(?:  +|\t)\S.*\n(?:.*\n)*?(?:  +|\t)\S", re.MULTILINE),  # indented lines
    re.compile(r"^\s*(?:return|if|elif|else|for|while|try|except|catch)\b.*[:({]\s*$", re.MULTILINE),
    re.compile(r"^\s*(?://|/\*|\*/)", re.MULTILINE),  # a comment line
]


def _is_code(text: str) -> bool:
    """The text has one strong sign of source code, or two different weak ones."""
    if _STRONG_CODE.search(text):
        return True
    return sum(1 for pattern in _WEAK_CODE if pattern.search(text)) >= 2


# --- Address ---

_STREET_WORD = (
    r"(?:Road|Rd\.?|Street|St\.|Avenue|Ave\.?|Lane|Ln\.?|Boulevard|Blvd\.?|Nagar"
    r"|Colony|Marg|Layout|Sector|Block|Apartments?|Apt\.?|Suite|Floor|Plot|Flat)"
)
# A street word that follows another word ("MG Road"), or a PO box.
_ADDRESS = re.compile(r"\w\s+" + _STREET_WORD + r"(?=[\s,.]|$)|\bP\.?\s?O\.? Box \d", re.IGNORECASE)
_MAX_ADDRESS_WORDS = 30


def _is_address(text: str) -> bool:
    """Short text with a number and a street-type word after another word."""
    if len(text.split()) > _MAX_ADDRESS_WORDS:
        return False
    return any(character.isdigit() for character in text) and _ADDRESS.search(text) is not None


# --- The ordered rule list ---

# Order matters: the first rule that matches wins. Whole-text formats come
# first, then formats recognised by how they start, then the looser ones.
_RULES: list[tuple[Label, Callable[[str], bool]]] = [
    (Label.LINK, _is_link),
    (Label.JSON, _is_json),
    (Label.ERROR_MESSAGE, _is_error_message),
    (Label.SQL, _is_sql),
    (Label.ENV_VAR, _is_env_var),
    (Label.FILE_PATH, _is_file_path),
    (Label.IP_ADDRESS, _is_ip_address),
    (Label.HASH, _is_hash),
    (Label.SHELL_COMMAND, _is_shell_command),
    (Label.CODE, _is_code),
    (Label.ADDRESS, _is_address),
]


def classify(text: str) -> Classification:
    """Return the content-type label for a piece of clipboard text."""
    stripped = text.strip()
    if not stripped:
        return Classification(Label.NOTE)
    head = stripped[:MAX_ANALYZED_CHARS]
    for label, matches in _RULES:
        # JSON must be parsed whole; every other rule looks at the head only.
        if matches(stripped if label is Label.JSON else head):
            domain = _link_domain(head) if label is Label.LINK else None
            return Classification(label, domain)
    return Classification(Label.NOTE)
