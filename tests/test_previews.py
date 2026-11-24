"""Rich previews by type (UI plan item 8), the data side. Made-up strings only, no Qt."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from smartclip.labels import Label, SecretState
from smartclip.storage import ItemPayload, StoredItem
from smartclip.ui.view_model import (
    COLLAPSED_LINES,
    EXPANDED_LINES,
    LONG_TEXT_CHARS,
    MAX_PREVIEW_INPUT,
    PREVIEW_CHARS,
    RevealState,
    Seg,
    build_rows,
    preview_for,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def texts(preview) -> list[list[str]]:  # noqa: ANN001
    return [[seg.text for seg in line] for line in preview.lines]


# --- Links ---


def test_link_shows_the_domain_bold_and_the_rest_muted() -> None:
    p = preview_for(Label.LINK, "https://github.com/example/repo/pull/42")
    assert p.kind == "link"
    domain, rest = p.lines[0]
    assert domain == Seg("github.com", "bold")
    assert rest == Seg("/example/repo/pull/42", "muted", "middle")


def test_link_without_a_scheme_or_path() -> None:
    assert texts(preview_for(Label.LINK, "example.test")) == [["example.test", ""]]
    assert texts(preview_for(Label.LINK, "  http://example.test:8080/x?y=1#z \n")) == [
        ["example.test:8080", "/x?y=1#z"]
    ]


@pytest.mark.parametrize("text", ["see https://example.test/x", "https://example.test/a b", "", "   "])
def test_link_label_on_text_that_is_not_a_link_stays_plain(text: str) -> None:
    assert preview_for(Label.LINK, text).kind == "plain"


# --- JSON ---


def test_json_collapses_to_its_keys() -> None:
    p = preview_for(Label.JSON, '{"name": "fake", "retries": 3, "debug": false}')
    assert p.kind == "json" and texts(p) == [["{ name, retries, debug }"]]
    assert p.expandable and not p.expanded


def test_json_with_many_keys_shows_the_first_few_and_an_ellipsis() -> None:
    data = "{" + ",".join(f'"k{n}": {n}' for n in range(12)) + "}"
    summary = texts(preview_for(Label.JSON, data))[0][0]
    assert summary.startswith("{ k0, k1") and summary.endswith(", \u2026 }") and "k8" not in summary


def test_json_show_more_gives_pretty_printed_mono_lines() -> None:
    p = preview_for(Label.JSON, '{"a": {"b": 1}, "c": [1, 2]}', expanded=True)
    assert p.expanded and p.lines[0][0] == Seg("{", "mono")
    assert all(line[0].style == "mono" for line in p.lines)
    assert any('"b": 1' in line[0].text for line in p.lines)


def test_json_pretty_view_is_capped() -> None:
    data = "{" + ",".join(f'"k{n}": {n}' for n in range(40)) + "}"
    p = preview_for(Label.JSON, data, expanded=True)
    assert len(p.lines) == EXPANDED_LINES and p.lines[-1][0].text == "\u2026"


@pytest.mark.parametrize("text", ["{not json", "[1, 2, 3]", "{}", '"text"', "42"])
def test_json_label_on_other_text_stays_plain(text: str) -> None:
    assert preview_for(Label.JSON, text).kind == "plain"


def test_very_large_json_is_not_parsed() -> None:
    big = '{"a": "' + "x" * 30000 + '"}'
    assert preview_for(Label.JSON, big).kind == "plain"


# --- Code, shell, SQL ---


def test_code_keeps_up_to_four_mono_lines() -> None:
    code = "def fake(a):\n    x = a + 1\n    y = x * 2\n    return y"
    p = preview_for(Label.CODE, code)
    assert p.kind == "code" and len(p.lines) == 4 and not p.expandable
    assert all(line[0].style == "mono" for line in p.lines)
    assert p.lines[1][0].text == "    x = a + 1"  # indentation kept


def test_longer_code_offers_show_more_and_cuts_with_an_ellipsis() -> None:
    code = "\n".join(f"line {n}" for n in range(20))
    p = preview_for(Label.CODE, code)
    assert len(p.lines) == COLLAPSED_LINES and p.expandable and p.lines[-1][0].text.endswith("\u2026")
    opened = preview_for(Label.CODE, code, expanded=True)
    assert len(opened.lines) == EXPANDED_LINES and opened.expanded


def test_code_tabs_blank_edges_and_trailing_space_are_tidied() -> None:
    p = preview_for(Label.SQL, "\n\n\tSELECT 1;   \n\n")
    assert texts(p) == [["    SELECT 1;"]]
    assert preview_for(Label.CODE, "  \n \n").kind == "plain"


@pytest.mark.parametrize("label", [Label.CODE, Label.SHELL_COMMAND, Label.SQL])
def test_all_code_like_labels_use_mono(label: Label) -> None:
    assert preview_for(label, "echo fake").lines[0][0].style == "mono"


# --- Paths ---


def test_path_shows_folder_muted_name_bold_and_an_extension_chip() -> None:
    p = preview_for(Label.FILE_PATH, r"C:\Users\demo\Documents\notes.txt")
    folder, name = p.lines[0]
    assert folder == Seg("C:\\Users\\demo\\Documents\\", "muted", "middle")
    assert name == Seg("notes.txt", "bold") and p.chip == "txt"


def test_path_with_forward_slashes_and_no_folder() -> None:
    assert texts(preview_for(Label.FILE_PATH, "/home/fake/run.PY")) == [["/home/fake/", "run.PY"]]
    assert preview_for(Label.FILE_PATH, "/home/fake/run.PY").chip == "py"
    only = preview_for(Label.FILE_PATH, "readme.md")
    assert texts(only) == [["readme.md"]] and only.chip == "md"


@pytest.mark.parametrize(
    "path, chip",
    [(r"C:\fake\Makefile", None), (r"C:\fake\.gitignore", None), (r"C:\fake\a.verylongext", None)],
)
def test_path_chip_only_for_a_short_extension(path: str, chip: str | None) -> None:
    assert preview_for(Label.FILE_PATH, path).chip == chip


@pytest.mark.parametrize("text", [r"C:\fake\folder" + "\\", "C:\\a\nC:\\b", ""])
def test_path_label_on_other_text_stays_plain(text: str) -> None:
    assert preview_for(Label.FILE_PATH, text).kind == "plain"


# --- Errors ---


def test_error_shows_the_first_line_bold_then_muted_lines() -> None:
    err = "Traceback (most recent call last):\n  File \"a.py\", line 1\nKeyError: 'fake'"
    p = preview_for(Label.ERROR_MESSAGE, err)
    assert p.lines[0] == (Seg("Traceback (most recent call last):", "bold"),)
    assert p.lines[1] == (Seg('File "a.py", line 1', "muted"),)
    assert len(p.lines) == 3 and not p.expandable


def test_long_error_offers_show_more() -> None:
    err = "\n".join(f"line {n}" for n in range(30))
    p = preview_for(Label.ERROR_MESSAGE, err)
    assert len(p.lines) == COLLAPSED_LINES - 1 and p.expandable
    assert len(preview_for(Label.ERROR_MESSAGE, err, expanded=True).lines) == EXPANDED_LINES


# --- Colours and plain text ---


@pytest.mark.parametrize("label", [Label.NOTE, Label.ENV_VAR, Label.HASH])
def test_a_hex_colour_gets_a_swatch_whatever_its_label(label: Label) -> None:
    p = preview_for(label, " #FF8800 ")
    assert p.kind == "colour" and p.swatch == "#ff8800" and texts(p) == [["#FF8800"]]


@pytest.mark.parametrize("text", ["#ff880", "#ff88000", "#gg8800", "color #ff8800"])
def test_other_hash_text_is_not_a_colour(text: str) -> None:
    assert preview_for(Label.NOTE, text).swatch is None


def test_short_note_is_plain_and_not_expandable() -> None:
    p = preview_for(Label.NOTE, "Meeting moved\nto five")
    assert p.kind == "plain" and p.wrap and texts(p) == [["Meeting moved to five"]]
    assert not p.expandable


def test_long_note_expands_to_more_text_and_stops_at_the_limit() -> None:
    note = "word " * 400
    closed = preview_for(Label.NOTE, note)
    assert closed.expandable and len(texts(closed)[0][0]) <= PREVIEW_CHARS
    opened = preview_for(Label.NOTE, note, expanded=True)
    shown = texts(opened)[0][0]
    assert opened.expanded and PREVIEW_CHARS < len(shown) <= LONG_TEXT_CHARS
    assert shown.endswith("\u2026")


def test_a_note_just_over_the_preview_length_expands_to_all_of_it() -> None:
    note = "x" * (PREVIEW_CHARS + 10)
    assert texts(preview_for(Label.NOTE, note, expanded=True)) == [[note]]


# --- Safety and speed ---


def test_huge_input_is_cut_before_anything_looks_at_it() -> None:
    start = time.perf_counter()
    for label in Label:
        preview_for(label, "a" * 1_000_000)
        preview_for(label, ("a/" * 500_000) + "!")
        preview_for(label, "\n".join(["line"] * 200_000))
    assert time.perf_counter() - start < 3.0
    assert len(texts(preview_for(Label.CODE, "x\n" * 1_000_000))) <= COLLAPSED_LINES
    assert MAX_PREVIEW_INPUT == 4000


def test_awkward_link_text_is_linear() -> None:
    start = time.perf_counter()
    preview_for(Label.LINK, "a" * 3000 + "://" + "b" * 100000)
    preview_for(Label.LINK, "://" * 50000)
    assert time.perf_counter() - start < 0.5


# --- In rows ---


def _item(item_id: int, payload: ItemPayload) -> StoredItem:
    return StoredItem(item_id, NOW - timedelta(minutes=item_id), payload)


def test_normal_rows_carry_a_preview_and_keep_the_one_line_preview_text() -> None:
    [row] = build_rows(
        [_item(1, ItemPayload("https://example.test/fake", Label.LINK))], "", None, RevealState(), NOW
    )
    assert row.rich is not None and row.rich.kind == "link"
    assert row.preview == "https://example.test/fake"


def test_masked_and_revealed_secrets_never_get_a_preview() -> None:
    secret = _item(
        2,
        ItemPayload(
            "FAKE-preview-secret", Label.POSSIBLE_SECRET, secret_state=SecretState.SECRET,
            expires_at=NOW + timedelta(hours=1),
        ),
    )
    unsure = _item(
        3,
        ItemPayload(
            "https://example.test/fake-unsure", Label.LINK, secret_state=SecretState.UNSURE,
            expires_at=NOW + timedelta(hours=1),
        ),
    )
    reveals = RevealState()
    for row in build_rows([secret, unsure], "", None, reveals, NOW):
        assert row.rich is None and row.masked
    reveals.reveal(2, NOW)
    reveals.reveal(3, NOW)
    for row in build_rows([secret, unsure], "", None, reveals, NOW):
        assert row.revealed and row.rich is None


def test_expanded_ids_open_their_previews() -> None:
    long_note = _item(5, ItemPayload("word " * 300, Label.NOTE))
    [closed] = build_rows([long_note], "", None, RevealState(), NOW)
    [opened] = build_rows([long_note], "", None, RevealState(), NOW, expanded=frozenset({5}))
    assert closed.rich is not None and not closed.rich.expanded
    assert opened.rich is not None and opened.rich.expanded
