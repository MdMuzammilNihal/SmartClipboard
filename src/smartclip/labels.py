"""Content-type labels and secret outcomes shared across the app."""

from enum import StrEnum


class Label(StrEnum):
    """The one content-type label each clipboard item gets."""

    LINK = "link"
    CODE = "code"
    JSON = "json"
    SHELL_COMMAND = "shell_command"
    FILE_PATH = "file_path"
    ENV_VAR = "env_var"
    ERROR_MESSAGE = "error_message"
    SQL = "sql"
    IP_ADDRESS = "ip_address"
    HASH = "hash"
    ADDRESS = "address"
    NOTE = "note"
    POSSIBLE_SECRET = "possible_secret"


class SecretState(StrEnum):
    """The three outcomes of secret detection.

    This is separate from the label because any item can carry a secret:
    a JSON blob or a shell command may have a token inside it.
    """

    NORMAL = "normal"
    SECRET = "secret"
    UNSURE = "unsure"
