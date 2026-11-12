"""Secret detection by rules: estimates how likely a piece of text is a secret.

A password is just text, so this can only estimate. It combines several
signals and returns one of three outcomes instead of a yes or no:

- SECRET: the text contains a known key format (a token prefix, a private-key
  header, a card number that passes the Luhn check, a URL with a password in
  it), or it scores high AND came from a login-type app.
- UNSURE: it scores in the middle or above without a login-type source. The
  app shows a "Review?" badge and uses the short secret retention.
- NORMAL: everything else.

How the score is built (0 to 1):

- A single token with no spaces is scored on its shape: how many character
  classes it mixes (lower, upper, digit, symbol), its entropy, and its length.
  Entropy alone is weak on short strings, so it is never used alone.
- A token built on a common password word ("football1", "P@ssw0rd!") gets a
  large bonus. This is what catches weak passwords that have low entropy.
- A value assigned to a secret-sounding name (`DB_PASSWORD=...`,
  `"token": "..."`, `--api-key ...`) is scored even inside longer text.
- Text that looks like something harmless is not scored at all: UUIDs,
  hashes, emails, file names, order-style IDs, plain words and identifiers.

Known limits, stated honestly:

- These rules and numbers are starting points. They have not been measured
  on real data; that is what Phase 2 is for.
- Hashes, order IDs and UUIDs are deliberately ignored, so a real secret that
  happens to look like one of them is missed.
- Passphrases made only of letters ("CorrectHorseBatteryStaple") are missed.
- Product names and identifiers that mix cases and digits can be flagged as
  UNSURE. tests/test_secret_rules.py lists known misses and false alarms.
- The list of common password words below is short. It is a signal, not a
  dictionary.

SECURITY REVIEW (owner):
- This module never logs, prints, or stores anything.
- The `reasons` it returns are fixed rule names written in this file, never
  pieces of the text, so they are safe to store and to show.
- Thresholds live in config.py, not here. The list of login-type apps is
  empty until the owner fills it, so today only a known format can produce
  SECRET; everything else that scores is UNSURE.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from smartclip import config
from smartclip.labels import Label, SecretState

# Only the start of very long text is examined.
MAX_ANALYZED_CHARS = 100_000

_MIN_TOKEN_CHARS = 6
_MIN_SHAPE_CHARS = 8
_MAX_TOKEN_CHARS = 128


@dataclass(frozen=True)
class SecretAssessment:
    """The outcome for one piece of text."""

    state: SecretState
    score: float
    reasons: tuple[str, ...] = ()


# --- Known formats: any one of these means SECRET ---

_KNOWN_FORMATS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ),
    ("sk_prefixed_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}")),
    # "://user:password@host"
    ("url_with_password", re.compile(r"://[^/\s:@]+:[^/\s:@]+@")),
]

# 13 to 19 digits, optionally grouped with spaces or hyphens.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _passes_luhn(digits: str) -> bool:
    """The checksum every real payment card number satisfies."""
    total = 0
    for position, character in enumerate(reversed(digits)):
        value = int(character)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _has_card_number(text: str) -> bool:
    """The text contains a card-length number that passes the Luhn check."""
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        # Card numbers start with 2 to 6; this skips many unrelated numbers.
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and _passes_luhn(digits):
            return True
    return False


def _known_formats(text: str) -> tuple[str, ...]:
    """Return the names of every known secret format found in the text."""
    found = [name for name, pattern in _KNOWN_FORMATS if pattern.search(text)]
    if _has_card_number(text):
        found.append("card_number")
    return tuple(found)


# --- Token shape ---

# Flagged even when they stand alone.
_KEYBOARD_PATTERNS = frozenset(
    """qwerty qwertyuiop asdfgh asdfghjkl zxcvbn zxcvbnm qazwsx 1qaz2wsx 1q2w3e4r
    letmein iloveyou abc123 123456 1234567 12345678 123456789 1234567890 111111
    000000 password1 trustno1""".split()
)
# Flagged only with digits or symbols added, because alone they are just words.
_COMMON_BASE_WORDS = frozenset(
    """password passwd welcome admin login secret changeme football baseball soccer
    cricket hockey dragon monkey master shadow sunshine princess superman batman
    summer winter spring autumn freedom whatever computer internet starwars pokemon
    hunter ranger buster killer charlie ginger pepper michael jordan robert thomas
    daniel jessica ashley nicole india test guest root user temp demo default""".split()
)
# Common letter substitutions: 0 for o, 1 for l, 3 for e, 4 for a, and so on.
_LEET = str.maketrans("0134578@$", "oleastbas")
_EDGE_PADDING = "0123456789!@#$%^&*._-+?"

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_HASH = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_FILE_NAME = re.compile(r"[\w\-.()]+\.[A-Za-z][A-Za-z0-9]{1,4}")
# "INV20261112", "ORD-000123"
_UPPER_CASE_ID = re.compile(r"[A-Z]{1,8}[-_#]?\d{3,}")
# Parts that are all letters or all digits, joined by punctuation:
# "ORD-2026-000123", "2026-11-12", "feature-login-page".
_SEGMENTED_ID = re.compile(r"(?:[A-Za-z]+|\d+)(?:[-_./:](?:[A-Za-z]+|\d+))+")
# "v1.2.3", "2.0.1-beta", "1.4.0+build5"
_VERSION = re.compile(r"v?\d+(?:\.\d+)+(?:[-+][\w.]+)?", re.IGNORECASE)
_HARMLESS_SHAPES = (_FILE_NAME, _UPPER_CASE_ID, _SEGMENTED_ID, _VERSION)
_IDENTIFIER_SYMBOLS = frozenset("_-.")


def _entropy(text: str) -> float:
    """Shannon entropy in bits per character: how evenly spread the characters are."""
    length = len(text)
    counts = Counter(text)
    return -sum(n / length * math.log2(n / length) for n in counts.values())


def _has_common_base(token: str) -> bool:
    """The token is a common password word with digits or symbols added."""
    lowered = token.lower()
    if lowered.isalpha():
        return False
    core = lowered.strip(_EDGE_PADDING)
    for candidate in (core, core.translate(_LEET)):
        if re.sub(r"[^a-z]", "", candidate) in _COMMON_BASE_WORDS:
            return True
    return False


def _token_points(token: str) -> tuple[int, tuple[str, ...]]:
    """Score one token with no spaces, from 0 to 100, with the rules that fired."""
    length = len(token)
    if length < _MIN_TOKEN_CHARS or length > _MAX_TOKEN_CHARS:
        return 0, ()
    if token.lower() in _KEYBOARD_PATTERNS:
        return 75, ("common_password",)
    if "://" in token or any(p.fullmatch(token) for p in (_UUID, _HASH, _EMAIL)):
        return 0, ()

    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    symbols = {c for c in token if not c.isalnum()}
    if not (has_lower or has_upper):
        return 0, ()  # no letters: phone numbers, dates, amounts

    common_base = _has_common_base(token)
    if not common_base:
        if length < _MIN_SHAPE_CHARS:
            return 0, ()
        if not has_digit and symbols <= _IDENTIFIER_SYMBOLS:
            return 0, ()  # plain words and identifiers such as getUserName
        if any(pattern.fullmatch(token) for pattern in _HARMLESS_SHAPES):
            return 0, ()

    points = 0
    reasons: list[str] = []
    class_count = sum([has_lower, has_upper, has_digit, bool(symbols)])
    points += {4: 50, 3: 35, 2: 15}.get(class_count, 0)
    if class_count >= 3:
        reasons.append("mixed_character_classes")
    entropy = _entropy(token)
    if entropy >= 3.0:
        points += 20
        reasons.append("high_entropy")
    if entropy >= 4.0:
        points += 10
    if length >= 12:
        points += 10
    if length >= 20:
        points += 10
        reasons.append("long_token")
    if common_base:
        points += 50
        reasons.append("common_password_base")
    return min(points, 100), tuple(reasons)


# --- A value assigned to a secret-sounding name ---

_SECRET_NAME = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|credential)"
)
# DB_PASSWORD=value   "token": "value"   ?api_key=value   the password is value
_NAMED_ASSIGNMENT = re.compile(
    _SECRET_NAME + r"[\w.-]{0,40}[\"']?(?:\s*[:=]\s*|\s+is\s+)[\"']?([^\s\"',;&}]{6,})",
    re.IGNORECASE,
)
# --password value   -token value
_NAMED_FLAG = re.compile(
    r"(?<![\w-])--?[\w-]{0,40}" + _SECRET_NAME + r"[\w-]{0,40}\s+[\"']?([^\s\"']{6,})",
    re.IGNORECASE,
)
_PLACEHOLDER_WORDS = frozenset(
    """changeme example placeholder redacted undefined null none true false
    password secret hidden""".split()
)
_PLACEHOLDER_STARTS = ("<", "$", "{{", "%", "your", "xxx", "***", "...")
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_.]*")


def _is_placeholder(value: str) -> bool:
    """The assigned value is a stand-in or a code expression, not a secret."""
    lowered = value.lower()
    return (
        lowered in _PLACEHOLDER_WORDS
        or lowered.startswith(_PLACEHOLDER_STARTS)
        or len(set(lowered)) <= 2  # "********", "xxxxxxxx"
        or any(character in value for character in "()[]")  # a function call or lookup
        or _PLAIN_IDENTIFIER.fullmatch(value) is not None  # os.environ, expired
    )


def _named_secret_value(text: str) -> str | None:
    """Return the first value assigned to a secret-sounding name, if any."""
    for pattern in (_NAMED_ASSIGNMENT, _NAMED_FLAG):
        for match in pattern.finditer(text):
            value = match.group(1)
            if not _is_placeholder(value):
                return value
    return None


# --- Putting it together ---


def _points(text: str, label: Label | None) -> tuple[int, tuple[str, ...]]:
    """Score the text from 0 to 100, with the rules that fired."""
    points, reasons = 0, ()
    is_single_token = not any(character.isspace() for character in text)
    # Shape scoring is for a lone token the classifier could not place.
    # A link, path, hash, or line of code is not judged by its shape.
    if is_single_token and label in (None, Label.NOTE):
        points, reasons = _token_points(text)

    value = _named_secret_value(text)
    if value is not None:
        value_points, _ = _token_points(value)
        named_points = 85 if value_points >= 45 else 60
        if named_points > points:
            points, reasons = named_points, ("secret_named_value",)
    return points, reasons


def assess(
    text: str,
    source_app: str | None = None,
    label: Label | None = None,
    thresholds: config.SecretThresholds = config.SECRET_THRESHOLDS,
    login_apps: frozenset[str] = config.LOGIN_SOURCE_APPS,
) -> SecretAssessment:
    """Estimate whether text is a secret. See the module docstring for the rules."""
    head = text.strip()[:MAX_ANALYZED_CHARS]
    if not head:
        return SecretAssessment(SecretState.NORMAL, 0.0)

    formats = _known_formats(head)
    if formats:
        return SecretAssessment(SecretState.SECRET, 1.0, formats)

    points, reasons = _points(head, label)
    score = points / 100
    from_login_app = source_app is not None and source_app.lower() in login_apps
    if score >= thresholds.high and from_login_app:
        return SecretAssessment(SecretState.SECRET, score, (*reasons, "login_source"))
    if score >= thresholds.unsure:
        return SecretAssessment(SecretState.UNSURE, score, reasons)
    return SecretAssessment(SecretState.NORMAL, score, reasons)
