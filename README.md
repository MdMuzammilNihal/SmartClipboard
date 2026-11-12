# Smart Clipboard

A local, privacy-focused smart clipboard for Windows. It watches what you copy, labels each item (link, code, note, possible secret, and so on), stores everything encrypted on your own machine, and lets you search, filter, and re-copy items from a hotkey window.

**It is not a password manager.** Do not store real passwords in it. Use a real password manager such as Bitwarden or KeePassXC for that. The value of this project is automatic sorting and labeling of everyday clipboard text.

## Status

**Early development. The app does not work yet.** This README describes the design that is being built. Nothing below has been verified on a real machine unless the table says so.

| Part | State |
|---|---|
| Repository, pinned dependencies, package skeleton | Done |
| Test that fails if app code imports a networking module | Done |
| Clipboard listener | Not started |
| Encrypted storage | Not started |
| Content labeling and secret flagging | Not started |
| Privacy controls (pause, exclusions, opt-out formats) | Not started |
| Retention and duplicate merging | Not started |
| Hotkey search window | Not started |
| Safe copy-back for secrets | Not started |

This table is updated as work lands.

## Why

Windows' built-in clipboard history (Win+V) keeps about 25 items, has no real search or labels, and is wiped on restart unless items are pinned.

Other clipboard managers already cover history, and some cover secret detection. This project does not claim to be better than them. What it aims to offer:

- A system-wide Windows desktop watcher, not a browser extension
- Labels that use the source app as a hint
- A measured evaluation of how well detection works (planned for Phase 2)
- An honest threat model, below

## How it is designed to work

1. **Watch.** A background listener receives clipboard-change events from Windows. Text only.
2. **Gate.** The item is skipped if capture is paused, if the source app is on your exclusion list, or if the clipboard carries the Windows opt-out formats that password managers set. The app also ignores its own clipboard writes.
3. **Classify.** Rules assign one content-type label and a secret score. The source app is recorded as a hint.
4. **Encrypt and store.** Each item becomes one payload (content, label, source app, flags, expiry), encrypted with Windows DPAPI and written to a local SQLite file.
5. **Apply retention.** Duplicates are merged. Normal items and secrets expire on different schedules.
6. **Retrieve.** A hotkey opens a small search-first window. Items show a colored type badge; clicking a badge filters by type. Secrets are masked and left out of search previews until you click to reveal them.
7. **Copy back safely.** Re-copying a secret keeps it out of Windows clipboard history and cloud sync where those formats are honored, and clears the clipboard after about 30 seconds.

## Content types

Labels are assigned by rules. The rules are starting points to be tested and refined, not final.

| Label | Example |
|---|---|
| Link | `https://github.com/...` |
| Code | `def foo(): ...` |
| JSON | `{"name": "x"}` |
| Shell command | `git commit -m ...` |
| File path | `C:\Users\me\file.txt` |
| Env var | `API_URL=https://...` |
| Error message | `Traceback (most recent call last)` |
| SQL | `SELECT * FROM users` |
| IP address | `192.168.1.10` |
| Hash | `e3b0c44298fc1c14...` |
| Address | `12 MG Road, Hyderabad` |
| Note | `Meeting at 5pm` |
| Possible secret | `Tr7!kd92x` |

JSON, shell commands, and env vars are also checked for secret patterns inside them.

## Secret detection

A password is just text, so the app can only estimate how likely something is a secret. It combines several signals instead of trusting one:

- **Shape:** one short token with no spaces, mixing uppercase, lowercase, digits, and symbols
- **Entropy plus a dictionary check.** Entropy alone is weak on short strings: `football1` has low entropy but is a password
- **Known formats:** prefixes such as `sk-`, `ghp_`, `AKIA`; JWTs; private-key headers; long hex or base64 strings; card numbers checked with the Luhn algorithm
- **Source app:** treated as a hint, not a fact

There are three outcomes rather than a yes or no:

| Outcome | What the app does |
|---|---|
| Clearly normal | Stored with its label and normal retention |
| Clearly secret | Flagged, masked, short retention |
| Unsure | Stored with a "Review?" badge and the short secret retention until you confirm it or mark it normal |

Missing a real secret is worse than a false alarm, so thresholds lean toward catching secrets. Thresholds live in config, not code.

**Known weak spots.** Hashes, order IDs, UUIDs, and long random filenames look like passwords and will cause false alarms. Memorable weak passwords look like normal words and can be missed. Rule-based detection will make both kinds of mistake.

## Threat model

Stored items are encrypted with Windows DPAPI (`CryptProtectData`). No custom cryptography is used.

| DPAPI protects against | DPAPI does not protect against |
|---|---|
| Someone copying the database file | Malware running under your Windows account |
| Another user account on the same PC | Anyone logged in as you |
| Casual snooping at the disk level | Someone who knows your Windows password |

Other limits you should know:

- **Data is tied to your Windows account.** A profile reset, or moving the database to another PC, makes it unreadable. There is no recovery.
- **Some metadata stays visible.** Each row's ID and creation time are stored in plaintext, so someone with the file can see how many items exist and when they were saved. Content, labels, source app, and flags are inside the encrypted payload.
- **Secrets cannot be reliably wiped from memory.** Python gives no dependable way to erase a string from RAM. While the app runs, decrypted items are in memory so search can work.
- **The Windows opt-out formats are cooperative.** Windows documents three clipboard formats for keeping data out of history and cloud sync: `ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory`, and `CanUploadToCloudClipboard`. This app honors them when watching and sets them when copying a secret back. Another clipboard manager is free to ignore them, so they are a courtesy, not a guarantee.
- **Password managers are skipped only if they cooperate, or if you exclude them.** A password manager that does not set the opt-out formats will be captured unless you add it to the exclusion list. The default exclusion list is empty.
- **Source-app detection is a best guess.** The app asks Windows which program owns the clipboard and falls back to the foreground window. Either can be wrong.

## Privacy rules the code follows

- **No network access.** No cloud APIs, telemetry, update checks, or analytics. A test fails if app code imports a networking module. That test is a static check of this project's own import statements: it does not catch dynamic imports and does not inspect what third-party dependencies do internally, so it is a guard against accidents, not proof.
- **Clipboard content is never logged.** Logs may contain IDs, content types, lengths, and timestamps only. This includes error messages and crash output.
- **Only app names are stored as context,** never window titles.
- **Tests use fake data only.**
- **Pause mode** stops capture entirely, with a visible indicator.

## Storage and retention

- The database lives under `%LOCALAPPDATA%\SmartClipboard\`, never inside the repository.
- Duplicates are detected with a keyed hash (HMAC-SHA256 with a random key that is itself DPAPI-protected). A plain hash of a short secret could be brute-forced by someone holding the file.
- A corrupted or tampered row is skipped and reported by ID. It must not crash the app.

| Item | Default | Configurable |
|---|---|---|
| Normal items | Deleted after 90 days | Yes |
| Secrets and unsure items | Deleted after 2 hours | Yes |
| Pinned normal items | Kept until unpinned | |
| Secrets | Cannot be pinned | |

## Default hotkeys

| Action | Keys |
|---|---|
| Open the search window | `Ctrl+Alt+V` |
| Pause or resume capture | `Ctrl+Alt+P` |
| Mark the last item as secret | `Ctrl+Alt+S` |

These are configurable. They have not yet been tested for clashes with other apps.

## Out of scope for this version

- Images, files, PDFs, and rich text (text only for now)
- Cloud or browser sync, browser extension, mobile
- Master-password vault and auto-lock
- AI summarization, snippets, text transforms, scripting
- A packaged `.exe`

## Requirements

- Windows 10 or 11, native (not WSL). Developed on Windows 11; Windows 10 is untested.
- Python 3.11 or newer. Developed on Python 3.13.

Dependencies are pinned: `pywin32` for the Windows APIs, `PySide6` for the window, and `pytest` for development.

## Development setup

In PowerShell, from the repository folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

Add `ui` to the extras (`".[dev,ui]"`) to install PySide6 as well. It is large and is not needed until the window is built.

## Roadmap

| Phase | What | Done when |
|---|---|---|
| 1 | Everything described above, with rule-based detection | Used daily for a week with no lost items; tests pass |
| 2 | Trained classifiers compared against the Phase 1 rules, then images, files, and PDFs | A real test set and before/after numbers exist |
| 3 | Semantic search compared with keyword search | Measured gain on test queries |
| 4 | Optional master-password vault and auto-lock | Round-trip and tamper tests pass |
| 5 | Packaging | A shareable build |

Phase 2 exists to replace "the detector works" with numbers. If the rules turn out to beat the trained model, the write-up will say so. Any real clipboard data used for evaluation stays private and is never published.

## License

No license has been chosen yet. Until one is added, all rights are reserved.
