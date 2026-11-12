"""Tests for secret detection. Every value is made up.

Token-shaped fakes are assembled from pieces at run time (for example
"sk_" + "test_" + "FAKE" * 5) so that no complete token-shaped string appears
in this file. That keeps automated secret scanners from mistaking these fakes
for leaked credentials.

Groups:
- KNOWN_FORMATS: must be SECRET, with the named rule.
- UNSURE_ITEMS: must be UNSURE (and never NORMAL).
- NORMAL_ITEMS: confusers that must stay NORMAL.
- KNOWN_MISSES / KNOWN_FALSE_ALARMS: what the rules get wrong today, marked
  as expected failures so the list stays honest.
"""

import base64
import io
import logging
import time

import pytest

from smartclip import config, logsafe
from smartclip.classify import classify
from smartclip.labels import Label, SecretState
from smartclip.secret_rules import assess

FAKE_LOGIN_APP = "fakemanager.exe"
FAKE_LOGIN_APPS = frozenset({FAKE_LOGIN_APP})


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


FAKE_JWT = _b64(b'{"alg":"none"}') + "." + _b64(b'{"sub":"fake"}') + "." + "FAKE" * 3
FAKE_GITHUB_TOKEN = "ghp_" + "FAKE" * 9
FAKE_PASSWORD = "Xk9#mQ2$vL7!"  # random-looking and made up; nobody's password

KNOWN_FORMATS: list[tuple[str, str]] = [
    ("private_key_block", "-----BEGIN " + "RSA PRIVATE KEY-----\nFAKEFAKE\n"),
    ("private_key_block", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ("private_key_block", "-----BEGIN " + "PRIVATE KEY-----"),
    ("jwt", FAKE_JWT),
    ("aws_access_key_id", "AKIA" + "FAKE" * 4),
    ("github_token", FAKE_GITHUB_TOKEN),
    ("github_token", "github_pat_" + "FAKE_" * 5),
    ("sk_prefixed_key", "sk-" + "FAKE" * 6),
    ("slack_token", "xoxb-" + "FAKE-0000-FAKE"),
    ("google_api_key", "AIza" + "FAKE" * 8 + "FAK"),
    ("stripe_key", "sk_" + "test_" + "FAKE" * 5),
    ("url_with_password", "https://fakeuser:FAKEpass@example.com/db"),
    # Published test card numbers; they pass the Luhn check but are not real cards.
    ("card_number", "4111 1111 1111 1111"),
    ("card_number", "4242424242424242"),
    ("card_number", "5555-5555-5555-4444"),
    # A known format inside other content.
    ("jwt", 'curl -H "Authorization: Bearer ' + FAKE_JWT + '" https://api.example.com'),
    ("github_token", "GITHUB_TOKEN=" + FAKE_GITHUB_TOKEN),
    ("github_token", '{"user": "fake", "auth": "' + FAKE_GITHUB_TOKEN + '"}'),
    ("card_number", "Card: 4111 1111 1111 1111 exp 01/30"),
]

UNSURE_ITEMS: list[str] = [
    "Tr7!kd92x",
    FAKE_PASSWORD,
    "football1",  # low entropy, but a common password word plus a digit
    "P@ssw0rd!",
    "summer2024",
    "admin123",
    "MyDog2019",
    "qwerty",
    "abc123",
    "12345678",
    "Zm9vYmFyRkFLRWZha2VGQUtFZmFrZUZBS0VmYWtlMTIz",  # long base64-style token
    "DB_PASSWORD=" + FAKE_PASSWORD,
    '{"password": "Xk9mQ2vL7"}',
    "fakecli login --token Xk9mQ2vL7abc",
    "https://example.com/reset?token=Xk9mQ2vL7abc",
    "The password is Xk9mQ2vL7",
    "pwd: hunter2x",
]

NORMAL_ITEMS: list[str] = [
    # The known confusers.
    "123e4567-e89b-12d3-a456-426614174000",  # UUID
    "d41d8cd98f00b204e9800998ecf8427e",  # 32-character hash
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # 40-character hash
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # 64
    "ORD-2026-000123",
    "INV20261112",
    "report_final_v2.pdf",
    "IMG_20260101_123456.jpg",
    # Words, identifiers, and ordinary text.
    "hello",
    "password",
    "Meeting at 5pm",
    "getUserNameById",
    "user_profile_id",
    "feature-login-page",
    "sam.fake99@example.com",
    # Numbers, dates, versions.
    "+91-98765-43210",
    "9876543210",
    "2026-11-12",
    "12:30:45",
    "v1.2.3-beta",
    "a1b2c3d4",  # short commit id
    "1234 5678 9012 3456",  # card-shaped but fails the Luhn check
    # Other content types.
    "https://example.com/path?id=12345678",
    "C:\\Users\\me\\file.txt",
    "192.168.1.10",
    "SELECT * FROM users",
    # Secret-sounding names with a placeholder or code on the right-hand side.
    'password = input("Enter password: ")',
    "API_KEY=your_api_key_here",
    "PASSWORD=changeme",
    "password: ********",
    "TOKEN=${FAKE_TOKEN}",
    "token: expired",
    "self.token = token",
    "Password must be at least 8 characters",
    "the password is required",
]

# Real secrets the rules do not catch: (text, the state it should get).
KNOWN_MISSES: list[str] = [
    "CorrectHorseBatteryStaple",  # letters-only passphrase
    "blue-horse-42",  # words and digits joined by hyphens look like an ID
    "sunshine",  # a weak password that is also an ordinary word
]

# Harmless text the rules flag as UNSURE.
KNOWN_FALSE_ALARMS: list[str] = [
    "iPhone15Pro",  # product name mixing cases and digits
    "user_1234",  # database-style identifier built on a common word
    "spring2026",  # a season and a year
]


def assess_as_app_would(text: str, source_app: str | None = None):
    """Classify first, then assess, the way the capture pipeline will."""
    return assess(text, source_app=source_app, label=classify(text).label)


# --- Known formats ---


@pytest.mark.parametrize(("rule", "text"), KNOWN_FORMATS)
def test_known_format_is_secret(rule: str, text: str) -> None:
    result = assess_as_app_would(text)
    assert result.state is SecretState.SECRET
    assert rule in result.reasons
    assert result.score == 1.0


def test_known_format_needs_no_login_source() -> None:
    assert assess(FAKE_GITHUB_TOKEN, source_app=None).state is SecretState.SECRET


# --- The unsure band ---


@pytest.mark.parametrize("text", UNSURE_ITEMS)
def test_likely_secret_is_unsure_without_a_login_source(text: str) -> None:
    result = assess_as_app_would(text)
    assert result.state is SecretState.UNSURE
    assert result.reasons


@pytest.mark.parametrize("text", NORMAL_ITEMS)
def test_confuser_stays_normal(text: str) -> None:
    assert assess_as_app_would(text).state is SecretState.NORMAL


@pytest.mark.parametrize("text", KNOWN_MISSES)
@pytest.mark.xfail(strict=True, reason="known miss of the current rules")
def test_known_miss(text: str) -> None:
    assert assess_as_app_would(text).state is not SecretState.NORMAL


@pytest.mark.parametrize("text", KNOWN_FALSE_ALARMS)
@pytest.mark.xfail(strict=True, reason="known false alarm of the current rules")
def test_known_false_alarm(text: str) -> None:
    assert assess_as_app_would(text).state is SecretState.NORMAL


def test_low_entropy_password_is_caught_by_the_word_list_not_entropy() -> None:
    result = assess("football1")
    assert "common_password_base" in result.reasons
    assert "high_entropy" not in result.reasons


# --- Source app as a hint ---


def test_high_score_from_a_login_app_is_secret() -> None:
    result = assess(FAKE_PASSWORD, source_app=FAKE_LOGIN_APP, login_apps=FAKE_LOGIN_APPS)
    assert result.state is SecretState.SECRET
    assert "login_source" in result.reasons


def test_login_app_name_is_matched_without_regard_to_case() -> None:
    result = assess(FAKE_PASSWORD, source_app="FakeManager.EXE", login_apps=FAKE_LOGIN_APPS)
    assert result.state is SecretState.SECRET


def test_high_score_from_another_app_stays_unsure() -> None:
    result = assess(FAKE_PASSWORD, source_app="fakeeditor.exe", login_apps=FAKE_LOGIN_APPS)
    assert result.state is SecretState.UNSURE


def test_middle_score_from_a_login_app_stays_unsure() -> None:
    result = assess("football1", source_app=FAKE_LOGIN_APP, login_apps=FAKE_LOGIN_APPS)
    assert config.SECRET_THRESHOLDS.unsure <= result.score < config.SECRET_THRESHOLDS.high
    assert result.state is SecretState.UNSURE


def test_login_app_does_not_make_ordinary_text_a_secret() -> None:
    result = assess("Meeting at 5pm", source_app=FAKE_LOGIN_APP, login_apps=FAKE_LOGIN_APPS)
    assert result.state is SecretState.NORMAL


def test_default_login_app_list_is_empty() -> None:
    # The owner has not chosen any yet, so only a known format can be SECRET.
    assert config.LOGIN_SOURCE_APPS == frozenset()
    assert assess(FAKE_PASSWORD, source_app=FAKE_LOGIN_APP).state is SecretState.UNSURE


# --- Thresholds come from config ---


def test_raising_the_unsure_threshold_makes_a_borderline_item_normal() -> None:
    strict = config.SecretThresholds(unsure=0.9, high=0.95)
    assert assess("football1").state is SecretState.UNSURE
    assert assess("football1", thresholds=strict).state is SecretState.NORMAL


def test_lowering_the_high_threshold_changes_the_login_app_outcome() -> None:
    lenient = config.SecretThresholds(unsure=0.3, high=0.6)
    result = assess(
        "football1", source_app=FAKE_LOGIN_APP, thresholds=lenient, login_apps=FAKE_LOGIN_APPS
    )
    assert result.state is SecretState.SECRET


def test_thresholds_never_hide_a_known_format() -> None:
    impossible = config.SecretThresholds(unsure=2.0, high=2.0)
    assert assess(FAKE_GITHUB_TOKEN, thresholds=impossible).state is SecretState.SECRET


# --- Shape scoring depends on the content type ---


def test_shape_is_not_scored_for_labels_other_than_note() -> None:
    # The same token is judged by shape only when the classifier found no type.
    assert assess(FAKE_PASSWORD, label=Label.NOTE).state is SecretState.UNSURE
    assert assess(FAKE_PASSWORD, label=Label.CODE).state is SecretState.NORMAL


def test_named_value_is_found_whatever_the_label() -> None:
    text = "DB_PASSWORD=" + FAKE_PASSWORD
    for label in (Label.ENV_VAR, Label.CODE, Label.SHELL_COMMAND, Label.NOTE):
        assert assess(text, label=label).state is SecretState.UNSURE


@pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
def test_empty_text_is_normal(empty: str) -> None:
    assert assess(empty).state is SecretState.NORMAL


# --- Safety and speed ---


def test_reasons_are_rule_names_never_content() -> None:
    for text in [FAKE_PASSWORD, "football1", "DB_PASSWORD=" + FAKE_PASSWORD, FAKE_JWT]:
        for reason in assess(text).reasons:
            assert reason.replace("_", "").isalpha()
            assert reason not in text


def test_assess_logs_nothing() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        for text in [*UNSURE_ITEMS, *NORMAL_ITEMS, *(text for _rule, text in KNOWN_FORMATS)]:
            assess_as_app_would(text)
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    assert stream.getvalue() == ""


@pytest.mark.parametrize("piece", ["a", "-", "1", "1 ", "password", "://", "eyJ", "--", "sk-", "="])
def test_long_repeated_input_is_assessed_quickly(piece: str) -> None:
    started = time.perf_counter()
    assess(piece * 200_000)
    assert time.perf_counter() - started < 2.0
