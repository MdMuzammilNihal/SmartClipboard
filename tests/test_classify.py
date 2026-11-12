"""Tests for the rule-based classifier. Every example is made up.

Three groups:
- EXAMPLES: text the rules are expected to label correctly.
- CONFUSERS: text that resembles another type and must not be mislabeled.
- KNOWN_MISLABELS: text the rules currently get wrong. Each is marked as an
  expected failure, so the list stays honest: if a rule change fixes one, the
  test run says so and the entry must be moved.
"""

import io
import logging
import time

import pytest

from smartclip import logsafe
from smartclip.classify import MAX_ANALYZED_CHARS, classify
from smartclip.labels import Label

EXAMPLES: list[tuple[str, Label]] = [
    # Link
    ("https://github.com/example/repo", Label.LINK),
    ("http://example.com/path?query=1#top", Label.LINK),
    ("www.example.org/docs", Label.LINK),
    ("  https://example.com  ", Label.LINK),
    ("ftp://files.example.com/pub/file.zip", Label.LINK),
    # JSON
    ('{"name": "x"}', Label.JSON),
    ('[1, 2, {"a": null}]', Label.JSON),
    ('{\n  "items": [],\n  "count": 0\n}', Label.JSON),
    ("{}", Label.JSON),
    # Error message
    ('Traceback (most recent call last):\n  File "fake.py", line 1, in <module>\nValueError: bad', Label.ERROR_MESSAGE),
    ("TypeError: cannot read properties of undefined", Label.ERROR_MESSAGE),
    ("error: failed to push some refs to 'origin'", Label.ERROR_MESSAGE),
    ("fatal: not a git repository", Label.ERROR_MESSAGE),
    ('Exception in thread "main" java.lang.NullPointerException\n\tat com.fake.App.main(App.java:10)', Label.ERROR_MESSAGE),
    ("npm ERR! code ENOENT", Label.ERROR_MESSAGE),
    ("error[E0308]: mismatched types", Label.ERROR_MESSAGE),
    ("[ERROR] connection refused", Label.ERROR_MESSAGE),
    ("ERROR 2026-01-01 12:00:00 fake.module failed to start", Label.ERROR_MESSAGE),
    # SQL
    ("SELECT * FROM users", Label.SQL),
    ("select id, name\nfrom customers\nwhere id = 5;", Label.SQL),
    ("INSERT INTO orders (id, total) VALUES (1, 9.99)", Label.SQL),
    ("UPDATE users SET name = 'x' WHERE id = 1", Label.SQL),
    ("DELETE FROM sessions WHERE expired = 1", Label.SQL),
    ("CREATE TABLE items (id INTEGER PRIMARY KEY)", Label.SQL),
    ("WITH recent AS (SELECT 1) SELECT * FROM recent", Label.SQL),
    ("SELECT 1;", Label.SQL),
    # Env var
    ("API_URL=https://api.example.com", Label.ENV_VAR),
    ("export NODE_ENV=production", Label.ENV_VAR),
    ("DEBUG=true\nPORT=8080", Label.ENV_VAR),
    ("# settings\nLOG_LEVEL=info\n\nTIMEOUT=30", Label.ENV_VAR),
    ('$env:FAKE_HOME = "C:\\fake"', Label.ENV_VAR),
    # File path
    ("C:\\Users\\me\\file.txt", Label.FILE_PATH),
    ("C:\\Program Files\\Fake App\\app.exe", Label.FILE_PATH),
    ('"D:/projects/demo"', Label.FILE_PATH),
    ("\\\\server\\share\\report.docx", Label.FILE_PATH),
    ("/usr/local/bin", Label.FILE_PATH),
    ("~/notes/todo.md", Label.FILE_PATH),
    ("./run.sh", Label.FILE_PATH),
    ("src/smartclip/classify.py", Label.FILE_PATH),
    # IP address
    ("192.168.1.10", Label.IP_ADDRESS),
    ("10.0.0.1:8080", Label.IP_ADDRESS),
    ("172.16.0.0/12", Label.IP_ADDRESS),
    ("::1", Label.IP_ADDRESS),
    ("2001:db8::ff00:42:8329", Label.IP_ADDRESS),
    ("[2001:db8::1]:443", Label.IP_ADDRESS),
    # Hash
    ("d41d8cd98f00b204e9800998ecf8427e", Label.HASH),
    ("da39a3ee5e6b4b0d3255bfef95601890afd80709", Label.HASH),
    ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", Label.HASH),
    ("D41D8CD98F00B204E9800998ECF8427E", Label.HASH),
    # Shell command
    ("git commit -m 'fake message'", Label.SHELL_COMMAND),
    ("pip install requests", Label.SHELL_COMMAND),
    ("$ docker ps -a", Label.SHELL_COMMAND),
    ("ls -la | grep fake", Label.SHELL_COMMAND),
    ("python script.py --verbose", Label.SHELL_COMMAND),
    ("cd /var/log && tail -f app.log", Label.SHELL_COMMAND),
    ("Get-ChildItem -Recurse", Label.SHELL_COMMAND),
    ("PS C:\\fake> npm run build", Label.SHELL_COMMAND),
    ("curl -X POST https://api.example.com/items -d '{\"a\": 1}'", Label.SHELL_COMMAND),
    # Code
    ("def foo():\n    return 1", Label.CODE),
    ("import os", Label.CODE),
    ("from pathlib import Path", Label.CODE),
    ("const total = items.length;", Label.CODE),
    ("function add(a, b) {\n  return a + b;\n}", Label.CODE),
    ("class Fake:\n    pass", Label.CODE),
    ("#include <stdio.h>\nint main() { return 0; }", Label.CODE),
    ("for (int i = 0; i < n; i++) {\n    total += i;\n}", Label.CODE),
    ("if x == 1:\n    print(x)", Label.CODE),
    ("<div class=\"fake\">hello</div>", Label.CODE),
    ("#!/bin/bash\necho hello", Label.CODE),
    ("MAX_RETRIES = 5\nprint(MAX_RETRIES)", Label.CODE),
    # Address
    ("12 MG Road, Hyderabad", Label.ADDRESS),
    ("221B Fake Street, London", Label.ADDRESS),
    ("Flat 4, Rose Apartments, Sector 21", Label.ADDRESS),
    ("742 Evergreen Avenue\nSpringfield 00000", Label.ADDRESS),
    ("PO Box 123, Faketown", Label.ADDRESS),
    # Note
    ("Meeting at 5pm", Label.NOTE),
    ("Remember to buy milk and call the dentist tomorrow.", Label.NOTE),
    ("hello", Label.NOTE),
    ("Thanks for the update, I will review it on Monday", Label.NOTE),
]

CONFUSERS: list[tuple[str, Label]] = [
    # Identifiers that look random. Until secret detection exists these are
    # Notes; that step must not flag them as secrets either.
    ("123e4567-e89b-12d3-a456-426614174000", Label.NOTE),  # UUID
    ("ORD-2026-000123", Label.NOTE),  # order ID
    ("INV20261112", Label.NOTE),
    # Hex of the wrong length is not a hash.
    ("abc123", Label.NOTE),
    ("d41d8cd98f00b204e9800998ecf8427", Label.NOTE),  # 31 characters
    # Dotted numbers that are not IP addresses.
    ("1.2.3", Label.NOTE),
    ("999.1.1.1", Label.NOTE),
    ("12:30", Label.NOTE),
    # Slashes that are not paths.
    ("12/05/2026", Label.NOTE),
    ("and/or", Label.NOTE),
    ("N/A", Label.NOTE),
    ("/help", Label.NOTE),
    # Sentences that start like a command.
    ("Make sure to call the bank", Label.NOTE),
    ("Go to the store", Label.NOTE),
    ("git is a version control system", Label.NOTE),
    ("Set up a meeting with the team", Label.NOTE),
    # Sentences that start like SQL.
    ("Update your profile picture", Label.NOTE),
    ("Create a table of contents", Label.NOTE),
    ("Delete the old files", Label.NOTE),
    # Sentences that mention code-like or error-like words.
    ("import duties are high this year", Label.NOTE),
    ("class action lawsuit filed", Label.NOTE),
    ("There was an error in my last message", Label.NOTE),
    # Things that look like env vars but are not.
    ("E=mc2", Label.NOTE),
    ("name=value", Label.NOTE),
    # Street words without an address.
    ("Drive 5 miles north", Label.NOTE),
    ("Meeting on Main Street", Label.NOTE),
    # Valid JSON that is only a number or a string is not labeled JSON.
    ("42", Label.NOTE),
    ('"just a quoted string"', Label.NOTE),
    # A link inside a sentence is a note, not a link.
    ("See https://example.com for details", Label.NOTE),
    # Punctuation that is not code.
    ("Call me (maybe) later; thanks", Label.NOTE),
]

# What the rules currently get wrong: (text, the label it should have).
KNOWN_MISLABELS: list[tuple[str, Label]] = [
    # A version number with four parts is also a valid IPv4 address.
    ("1.2.3.4", Label.NOTE),
    # English that happens to follow the SELECT ... FROM shape.
    ("Select all from the menu", Label.NOTE),
    # Commands with no flag, path, or file name are missed.
    ("go build", Label.SHELL_COMMAND),
    ("echo hello", Label.SHELL_COMMAND),
    # A lone folder name with one separator is missed.
    ("src/utils", Label.FILE_PATH),
    # An address with no street-type word is missed.
    ("14 Fakewood Close, Bristol", Label.ADDRESS),
    # One short line of code shows only one weak sign, and two are required.
    ("print('hello')", Label.CODE),
    ("x = 5", Label.CODE),
    (".btn { color: red; }", Label.CODE),
    # A web address with no "http://" or "www." is missed.
    ("example.com", Label.LINK),
    # Structured text with no rule of its own.
    ("name: fake\nversion: 1.0\ndeps:\n  - a\n  - b", Label.CODE),
]


@pytest.mark.parametrize(("text", "expected"), EXAMPLES)
def test_example_gets_its_label(text: str, expected: Label) -> None:
    assert classify(text).label is expected


@pytest.mark.parametrize(("text", "expected"), CONFUSERS)
def test_confuser_is_not_mislabeled(text: str, expected: Label) -> None:
    assert classify(text).label is expected


@pytest.mark.parametrize(("text", "expected"), KNOWN_MISLABELS)
@pytest.mark.xfail(strict=True, reason="known limit of the current rules")
def test_known_mislabel(text: str, expected: Label) -> None:
    assert classify(text).label is expected


def test_every_label_except_possible_secret_has_examples() -> None:
    covered = {label for _text, label in EXAMPLES}
    assert covered == set(Label) - {Label.POSSIBLE_SECRET}


def test_no_rule_assigns_possible_secret() -> None:
    # Secret detection is a separate step; a random token is a Note here.
    assert classify("Tr7!kd92x").label is Label.NOTE


@pytest.mark.parametrize("empty", ["", "   ", "\n\t\n"])
def test_empty_text_is_a_note(empty: str) -> None:
    assert classify(empty).label is Label.NOTE


# --- Link domain ---


@pytest.mark.parametrize(
    ("text", "domain"),
    [
        ("https://GitHub.com/example/repo", "github.com"),
        ("http://example.com:8080/path", "example.com"),
        ("www.example.org/docs", "www.example.org"),
        ("https://sub.example.co.uk?q=1", "sub.example.co.uk"),
        ("http://[2001:db8::1]:8080/x", "[2001:db8::1]"),
    ],
)
def test_link_records_the_domain(text: str, domain: str) -> None:
    result = classify(text)
    assert result.label is Label.LINK
    assert result.domain == domain


def test_credentials_in_a_url_are_not_kept_in_the_domain() -> None:
    result = classify("https://fakeuser:FAKEpass123@example.com/private")
    assert result.label is Label.LINK
    assert result.domain == "example.com"


def test_only_links_get_a_domain() -> None:
    assert classify("Meeting at 5pm").domain is None
    assert classify("git clone https://example.com/repo.git").domain is None


# --- Order of rules ---


def test_json_wins_over_code() -> None:
    assert classify('{"call": "foo()", "x": 1}').label is Label.JSON


def test_traceback_wins_over_the_code_inside_it() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "fake.py", line 3, in run\n'
        "    return compute(x)\n"
        "KeyError: 'fake'"
    )
    assert classify(text).label is Label.ERROR_MESSAGE


def test_code_that_raises_an_error_is_still_code() -> None:
    text = "def check(x):\n    if x < 0:\n        raise ValueError('negative')\n    return x"
    assert classify(text).label is Label.CODE


def test_code_with_spaced_constant_is_code_not_env_var() -> None:
    assert classify("MAX_RETRIES = 5\nprint(MAX_RETRIES)").label is Label.CODE


# --- Safety and speed ---


def test_classify_logs_nothing() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logsafe.attach_handler(handler)
    try:
        for text, _label in EXAMPLES:
            classify(text)
    finally:
        logging.getLogger(logsafe.ROOT_LOGGER_NAME).removeHandler(handler)
    assert stream.getvalue() == ""


def test_very_long_text_is_labeled_quickly() -> None:
    long_note = "FAKE words in a long note. " * 200_000  # about 5 million characters
    assert len(long_note) > MAX_ANALYZED_CHARS
    started = time.perf_counter()
    result = classify(long_note)
    assert result.label is Label.NOTE
    assert time.perf_counter() - started < 2.0


def test_long_run_of_one_character_is_labeled_quickly() -> None:
    for character in ("a", "(", "/", "=", " x", "{"):
        started = time.perf_counter()
        classify(character * 200_000)
        assert time.perf_counter() - started < 2.0
