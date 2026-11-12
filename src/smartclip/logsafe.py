"""Logging that cannot carry clipboard content.

SECURITY REVIEW (owner): the rule is that logs may hold IDs, content types,
lengths, and timestamps only. A rule that depends on every caller being
careful will eventually be broken, so this module enforces it in two places.

1. At the call site. Code logs through log_event() and log_error(), never
   through logger.info() directly. Field values may only be numbers, booleans,
   or None. Free strings are refused, with one exception: `content_type`,
   which must be a member of the Label enum. A refused field is dropped and
   counted (`dropped_fields=1`); it does not raise, because a logging mistake
   must never stop clipboard capture.

2. At the handler. SafeFormatter writes only text that log_event() built. If
   any code logs to a smartclip logger the ordinary way, the message, its
   arguments, and any attached traceback are discarded and a fixed marker is
   written instead.

Exceptions: an exception message can contain data (a parser quoting its
input, for example), so only the exception's class name is ever logged.
install_crash_handlers() replaces Python's default crash output, which would
print the message, with the class name and code locations (file, line,
function).

Known limits, stated honestly:

- The event name is a string. It is checked against a strict pattern
  (lowercase letters, digits, underscores), which stops most accidents, but a
  caller who passes clipboard text that happens to look like `hello` as the
  event name would get it logged. Event names must be literals in the code.
- This covers loggers under the "smartclip" name. It does not control what
  third-party libraries print, or a stray print() call in our own code.
"""

import logging
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from smartclip.labels import Label

ROOT_LOGGER_NAME = "smartclip"
LOG_FILE_NAME = "smartclip.log"
DROPPED_RECORD_TEXT = "unstructured_record_dropped"
INVALID_EVENT_TEXT = "invalid_event"

# Name of the attribute on a log record that holds the pre-built safe text.
_SAFE_TEXT_ATTR = "smartclip_safe_text"
_EVENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,47}")
_MAX_CRASH_FRAMES = 5
_MAX_LOG_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 3


def get_logger(module_name: str) -> logging.Logger:
    """Return the logger for one of our modules, for example "storage"."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{module_name}")


def log_event(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: object
) -> None:
    """Log an event with safe fields only (numbers, booleans, None, content_type)."""
    _emit(logger, level, event, _render_fields(fields))


def log_error(
    logger: logging.Logger, event: str, error: BaseException, **fields: object
) -> None:
    """Log a failure by exception class name only. The message is never logged."""
    rendered = [f"error_type={_error_type_name(type(error))}", *_render_fields(fields)]
    _emit(logger, logging.ERROR, event, rendered)


def _render_fields(fields: dict[str, object]) -> list[str]:
    """Turn fields into "name=value" strings, dropping any unsafe value."""
    rendered: list[str] = []
    dropped = 0
    for name, value in fields.items():
        text = _render_value(name, value)
        if text is None:
            dropped += 1
        else:
            rendered.append(f"{name}={text}")
    if dropped:
        rendered.append(f"dropped_fields={dropped}")
    return rendered


def _render_value(name: str, value: object) -> str | None:
    """Return the text for a safe value, or None if the value must be dropped."""
    if value is None:
        return "none"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    if name == "content_type" and isinstance(value, Label):
        return value.value
    return None


def _error_type_name(error_type: type[BaseException]) -> str:
    """Return an exception class name, which comes from code and not from data."""
    name = error_type.__name__
    return name if name.isidentifier() else "unknown"


def _emit(logger: logging.Logger, level: int, event: str, rendered: list[str]) -> None:
    """Send pre-built safe text to the logger."""
    is_valid = isinstance(event, str) and _EVENT_PATTERN.fullmatch(event) is not None
    safe_event = event if is_valid else INVALID_EVENT_TEXT
    text = " ".join([safe_event, *rendered])
    logger.log(level, text, extra={_SAFE_TEXT_ATTR: text})


class SafeFormatter(logging.Formatter):
    """Writes only text built by this module; everything else is discarded."""

    def format(self, record: logging.LogRecord) -> str:
        safe_text = getattr(record, _SAFE_TEXT_ATTR, None)
        if not isinstance(safe_text, str):
            # Someone logged the ordinary way. Do not write their message,
            # arguments, or traceback: any of them could hold content.
            safe_text = DROPPED_RECORD_TEXT
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        stamp = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{stamp} {record.levelname} {record.name} {safe_text}"


def attach_handler(handler: logging.Handler) -> None:
    """Send smartclip logs to a handler, forcing the safe formatter onto it."""
    handler.setFormatter(SafeFormatter())
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Do not pass records up to Python's root logger, whose handlers would
    # use their own formatters.
    root.propagate = False


def setup_logging(log_dir: Path) -> logging.Handler:
    """Start logging to a size-limited file in log_dir and return the handler."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    attach_handler(handler)
    return handler


def install_crash_handlers() -> None:
    """Replace Python's default crash output on every thread.

    The default output prints the exception message, which may hold content.
    Ours logs the exception class name and where in the code it happened.
    """
    logger = get_logger("crash")

    def report(error_type: type[BaseException], trace: TracebackType | None) -> None:
        if issubclass(error_type, KeyboardInterrupt):
            return
        rendered = [
            f"error_type={_error_type_name(error_type)}",
            f"where={_crash_location(trace)}",
        ]
        _emit(logger, logging.CRITICAL, "unhandled_exception", rendered)

    def main_thread_hook(
        error_type: type[BaseException], _error: BaseException, trace: TracebackType | None
    ) -> None:
        report(error_type, trace)

    def other_thread_hook(args: threading.ExceptHookArgs) -> None:
        report(args.exc_type, args.exc_traceback)

    sys.excepthook = main_thread_hook
    threading.excepthook = other_thread_hook


def _crash_location(trace: TracebackType | None) -> str:
    """Describe where a crash happened: file name, line, and function only.

    Uses file base names (a full path would include the Windows user name)
    and never reads local variables or source lines.
    """
    frames = [
        f"{Path(frame.f_code.co_filename).name}:{line_number}:{frame.f_code.co_name}"
        for frame, line_number in traceback.walk_tb(trace)
    ]
    return ">".join(frames[-_MAX_CRASH_FRAMES:]) or "unknown"
