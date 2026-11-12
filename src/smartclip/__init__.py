"""Smart Clipboard: a privacy-focused smart clipboard for Windows.

Not a password manager. See README.md for the threat model.

Project-wide rules that every module in this package must follow
(CLAUDE.md section 2):

- Clipboard content is never logged, printed, or written anywhere except the
  DPAPI-encrypted database. Logs may hold IDs, content types, lengths, and
  timestamps only.
- No network access of any kind. tests/test_no_network.py enforces this.
"""

__version__ = "0.1.0"
