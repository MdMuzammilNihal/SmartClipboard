"""Friendly names and tags for pinned items (UI plan item 9, owner's decision 2026-11-24).

A pinned, non-secret item can have a title and up to three tags. They live
inside the item's encrypted payload (storage.py), like the text itself, and
are never logged.

Rules (also enforced when a payload is built or loaded):

- Secret and Review? items can never have a title or tags: a title could
  reveal what the secret is. Marking an item secret clears them.
- A title is at most MAX_TITLE_CHARS characters, a tag at most MAX_TAG_CHARS,
  and an item has at most MAX_TAGS tags, with no repeats (compared ignoring case).
- Whitespace is tidied: runs of spaces and line breaks become one space.
"""

MAX_TITLE_CHARS = 60
MAX_TAGS = 3
MAX_TAG_CHARS = 24


def clean_title(text: str) -> str | None:
    """A tidy title, cut to the limit, or None if nothing is left."""
    tidy = " ".join(text.split())[:MAX_TITLE_CHARS].rstrip()
    return tidy or None


def clean_tag(text: str) -> str | None:
    """A tidy tag, or None if it is empty. Commas become spaces (tags are a list, not a string)."""
    tidy = " ".join(text.replace(",", " ").split())[:MAX_TAG_CHARS].rstrip()
    return tidy or None


def tag_key(tag: str) -> str:
    """How tags are compared: ignoring case."""
    return tag.casefold()


def add_tag(tags: tuple[str, ...], text: str) -> tuple[str, ...]:
    """The tags with `text` added. Unchanged if it is empty, repeated, or the list is full."""
    tag = clean_tag(text)
    if tag is None or len(tags) >= MAX_TAGS or tag_key(tag) in {tag_key(t) for t in tags}:
        return tags
    return (*tags, tag)


def remove_tag(tags: tuple[str, ...], text: str) -> tuple[str, ...]:
    """The tags without `text` (compared ignoring case)."""
    return tuple(tag for tag in tags if tag_key(tag) != tag_key(text))


def tags_are_valid(tags: tuple[str, ...]) -> bool:
    """True if `tags` obeys every limit and is already tidy."""
    return (
        len(tags) <= MAX_TAGS
        and all(clean_tag(tag) == tag for tag in tags)
        and len({tag_key(tag) for tag in tags}) == len(tags)
    )


def title_is_valid(title: str | None) -> bool:
    """True for no title, or a tidy title within the limit."""
    return title is None or clean_title(title) == title
