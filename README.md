# Smart Clipboard

A local, privacy-focused smart clipboard for Windows. It watches what you copy, labels each item (link, code, note, possible secret, and so on), stores everything encrypted on your own machine, and lets you search, filter, and re-copy items from a hotkey window.

**It is not a password manager.** Do not store real passwords in it. Use a real password manager such as Bitwarden or KeePassXC for that. The value of this project is automatic sorting and labeling of everyday clipboard text.

## Status

**Early development. The app does not work yet.** This README describes the design that is being built. Nothing below has been verified on a real machine unless the table says so.

| Part | State |
|---|---|
| Repository, pinned dependencies, package skeleton | Done |
| Test that fails if app code imports a networking module | Done |
| Encrypted storage (DPAPI + SQLite), with tests | Written and tested; not yet connected to a listener |
| Logging that refuses clipboard content, with tests | Written and tested |
| Clipboard listener (own thread, busy-retry, merging of repeated events) | Written; tested with a fake clipboard, real window messages, and opt-in tests that write fake text to the real clipboard (passing on the owner's Windows 11 machine). Passes every change through the privacy gate. Not yet connected to storage, and not yet confirmed against real copies from other apps |
| Content labeling by rules | Written and tested on made-up examples only; accuracy on real copies is not measured. Known mislabels are listed in `tests/test_classify.py` |
| Secret flagging by rules | Written and tested on made-up values only; thresholds are starting values, not measured. Known misses and false alarms are listed in `tests/test_secret_rules.py`. Not yet connected to capture |
| Privacy gate (pause, app exclusion list, Windows opt-out formats, ignoring the app's own writes) | Written and tested with a fake clipboard; opt-out formats and own-write skipping also pass opt-in real-clipboard tests with fake text. Pause has no hotkey or tray toggle yet (comes with the UI). Not yet checked against a real password manager |
| Manual overrides: force-save and mark-as-secret hotkeys | Hotkey registration, force-save, and the mark-as-secret rule are written and tested (force-save also on the real clipboard with fake text). Not usable yet: storage and the hotkeys are not connected to the app. Real key presses are untested |
| Source-app detection (clipboard owner, foreground window as fallback) | Written and tested with fake window lookups; how often it names the right app is not measured yet |
| Retention, duplicate merging, and pin rules | Written and tested with fake data on a temporary database: duplicates merge through a keyed hash and move to the top; normal items expire 90 days after the last copy; secrets and unsure items 2 hours after the first copy (copying again does not extend it); pinned normal items are kept; secrets and unsure items cannot be pinned. Not yet connected to the listener or run against real use |
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

"Clearly secret" means the text contains a known key format, or it scores high and was copied from an app on a list of login-type apps. That list is empty by default, so until you fill it, a random-looking password with no known format lands in "Unsure". Unsure items get the same short retention as secrets.

**Known weak spots.** Hashes, order IDs, UUIDs, and file names look like passwords, so the rules deliberately ignore those shapes; a real secret that happens to look like one of them is missed. Passphrases made only of letters are missed. Product names and identifiers that mix cases and digits (`iPhone15Pro`, `user_1234`) can be flagged for review. Rule-based detection will make both kinds of mistake.

## Threat model

Stored items are encrypted with Windows DPAPI (`CryptProtectData`). No custom cryptography is used.

| DPAPI protects against | DPAPI does not protect against |
|---|---|
| Someone copying the database file | Malware running under your Windows account |
| Another user account on the same PC | Anyone logged in as you |
| Casual snooping at the disk level | Someone who knows your Windows password |

Other limits you should know:

- **Data is tied to your Windows account.** A profile reset, or moving the database to another PC, makes it unreadable. There is no recovery.
- **Some metadata stays visible.** Each row's ID and creation time are stored in plaintext, so someone with the file can see how many items exist and when they were saved. The size of each encrypted payload is also visible, which roughly reveals how long the copied text was. Content, labels, source app, and flags are inside the encrypted payload.
- **Deleted rows are overwritten in the database file** (SQLite's `secure_delete`). This does not control copies the disk or Windows may keep elsewhere, such as SSD wear levelling, backups, or shadow copies.
- **Secrets cannot be reliably wiped from memory.** Python gives no dependable way to erase a string from RAM. While the app runs, decrypted items are in memory so search can work.
- **The Windows opt-out formats are cooperative.** Windows documents three clipboard formats for keeping data out of history and cloud sync: `ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory`, and `CanUploadToCloudClipboard`. This app honors them when watching and sets them when copying a secret back. Another clipboard manager is free to ignore them, so they are a courtesy, not a guarantee.
- **Password managers are skipped only if they cooperate, or if you exclude them.** A password manager that does not set the opt-out formats will be captured unless you add it to the exclusion list. The default exclusion list is empty.
- **Source-app detection is a best guess.** The app asks Windows which program owns the clipboard and falls back to the foreground window. Either can be wrong.
- **The app's own clipboard writes are recognised by the clipboard owner.** If the clipboard is owned by one of this app's windows, the change is not captured. This includes copying text inside the app's own window.

## Privacy rules the code follows

- **No network access.** No cloud APIs, telemetry, update checks, or analytics. A test fails if app code imports a networking module. That test is a static check of this project's own import statements: it does not catch dynamic imports and does not inspect what third-party dependencies do internally, so it is a guard against accidents, not proof.
- **Clipboard content is never logged.** Logs may contain IDs, content types, lengths, and timestamps only. This includes error messages and crash output. The logging module enforces this: it refuses free-text values, discards ordinary logging calls, and records only the class name of an exception, never its message. It cannot stop a stray `print()` or control what third-party libraries print.
- **Only app names are stored as context,** never window titles.
- **Tests use fake data only.**
- **Pause mode** stops capture entirely, with a visible indicator.

## Storage and retention

- The database lives under `%LOCALAPPDATA%\SmartClipboard\`, never inside the repository.
- Duplicates are detected with a keyed hash (HMAC-SHA256 with a random key that is itself DPAPI-protected). A plain hash of a short secret could be brute-forced by someone holding the file.
- A corrupted or tampered row is skipped and reported by ID. It must not crash the app.
- Copying the same text again merges it into the existing item and moves it to the top. The time of the latest copy is stored inside the encrypted payload; the readable creation time stays the time of the first copy.
- A row that can no longer be decrypted is deleted once its readable creation time is more than 90 days old, but only when other rows still decrypt. If no row decrypts, the problem is more likely the Windows account than damage, so nothing is deleted.

| Item | Default | Configurable |
|---|---|---|
| Normal items | Deleted 90 days after they were last copied | Yes |
| Secrets and unsure items | Deleted 2 hours after the first copy; copying again does not extend this | Yes |
| Pinned normal items | Kept until unpinned | |
| Secrets and unsure items | Cannot be pinned | |

## Default hotkeys

| Action | Keys |
|---|---|
| Open the search window | `Ctrl+Alt+V` |
| Pause or resume capture | `Ctrl+Alt+P` |
| Mark the last item as secret | `Ctrl+Alt+S` |
| Force-save what is on the clipboard now | `Ctrl+Alt+F` |

These are configurable. They have not yet been tested for clashes with other apps; if another app already holds a combination, it is reported instead of taken over.

- **Mark as secret** always marks secret, never back to normal, so a second press cannot undo a detection. It applies only while the clipboard still holds that item. Marking an item normal will be done in the window.
- **Force-save** captures the current clipboard even while capture is paused or the app is excluded. It still refuses content a password manager marked with the Windows opt-out formats.

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

MIT. See [LICENSE](LICENSE).
