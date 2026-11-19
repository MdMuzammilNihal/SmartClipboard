# Smart Clipboard

A local, privacy-focused smart clipboard for Windows. It watches what you copy, labels each item (link, code, note, possible secret, and so on), stores everything encrypted on your own machine, and lets you search, filter, and re-copy items from a hotkey window.

**It is not a password manager.** Do not store real passwords in it. Use a real password manager such as Bitwarden or KeePassXC for that. The value of this project is automatic sorting and labeling of everyday clipboard text.

## Status

**Early development.** The pieces are now wired into a first runnable app (`python -m smartclip`), but it has not yet been used on real copies from other apps. This README describes the design that is being built. Nothing below has been verified on a real machine unless the table says so.

| Part | State |
|---|---|
| Repository, pinned dependencies, package skeleton | Done |
| No network access (task 13) | Checked by automated tests: app code imports no networking module, and the app's flows, run with fake data, open no socket except loopback ones inside the process (see [Network](#network)). Manual check on the owner's Windows 11 machine (2026-11-19, fake text): no network connections seen while the app was used |
| Encrypted storage (DPAPI + SQLite), with tests | Written and tested; not yet connected to a listener |
| Logging that refuses clipboard content, with tests | Written and tested |
| Clipboard listener (own thread, busy-retry, merging of repeated events) | Written; tested with a fake clipboard, real window messages, and opt-in tests that write fake text to the real clipboard (passing on the owner's Windows 11 machine). Passes every change through the privacy gate. Not yet connected to storage, and not yet confirmed against real copies from other apps |
| Content labeling by rules | Written and tested on made-up examples only; accuracy on real copies is not measured. Known mislabels are listed in `tests/test_classify.py` |
| Secret flagging by rules | Written and tested on made-up values only; thresholds are starting values, not measured. Known misses and false alarms are listed in `tests/test_secret_rules.py`. Not yet connected to capture |
| Privacy gate (pause, app exclusion list, Windows opt-out formats, ignoring the app's own writes) | Written and tested with a fake clipboard; opt-out formats and own-write skipping also pass opt-in real-clipboard tests with fake text. Pause is toggled by `Ctrl+Alt+P` or the window's Pause button and shown in the window; there is no tray toggle yet, and the hotkey has not been pressed for real. Not yet checked against a real password manager |
| Manual overrides: force-save and mark-as-secret hotkeys | Hotkey registration, force-save, and the mark-as-secret rule are written and tested (force-save also on the real clipboard with fake text). Now connected to storage through `app.py`. Real key presses are untested |
| Source-app detection (clipboard owner, foreground window as fallback) | Written and tested with fake window lookups; how often it names the right app is not measured yet |
| Retention, duplicate merging, and pin rules | Written and tested with fake data on a temporary database: duplicates merge through a keyed hash and move to the top; normal items expire 90 days after the last copy; secrets and unsure items 2 hours after the first copy (copying again does not extend it); pinned normal items are kept; secrets and unsure items cannot be pinned. Not yet connected to the listener or run against real use |
| Hotkey search window (search, coloured type badges, click a badge to filter, pin, re-copy, delete, mark normal or secret, pause indicator) | Written; tested with a fake backend in an off-screen Qt window. Started on the owner's machine with capture paused and a throwaway data folder: it opened, registered its hotkeys, was marked as excluded from screen capture along with its tooltips and dialogs (read back from Windows; tooltips were added after the owner saw one in a screen recording), refused a second copy, and shut down cleanly. Hiding when another app takes the focus is tested with a fake focus check only. Not yet used by hand: real key presses, the window coming to the front over other apps, and how it looks have not been checked |
| Masked secrets (bullets, left out of search, revealed for 10 seconds by a click) | Written and tested with fake data, including a check that a masked row never hands its text to the window in any form. Not yet looked at on screen |
| Wiring (`app.py`: listener to labeling to storage, hotkeys, cleanup timer) | Written; the capture path is tested with fake events on a temporary database. Not yet run on real copies. Only one copy runs at a time. No tray icon or settings screen yet |
| Removing confirmed secrets from Win+V history | Written; tested with a fake history, and an opt-in test that puts fake text in the real Win+V history and removes it passes on the owner's Windows 11 machine. Hand-checked by the owner with fake text (2026-11-19): a captured secret leaves Win+V after the paste window, a Review? item marked secret leaves at once, and Mark normal cancels. Effect on cloud-synced devices not verified |
| Clearing secrets copied in other apps (30 seconds after capture, only if nothing was copied since; not for "Review?" items) | Written; tested with a fake clipboard and real window messages. Hand-tested by the owner on Windows 11 with fake text (2026-11-19): a captured secret is cleared, a newer copy is kept, "Review?" items are left alone, Ctrl+Alt+S starts the clear, and Mark normal cancels it. The opt-in automated real-clipboard test has not been run. Win+V history is not touched |
| Opt-in labeled-data log for Phase 2 (off by default) | Written; tested with fake data on a temporary file (redaction, encryption, damaged records, no content in logs, the app writing records on capture and marking). Not yet turned on or used on real copies |
| Safe copy-back for secrets | Written; tested with a fake clipboard and real window messages. Opt-in tests that write fake text to the real clipboard pass on the owner's Windows 11 machine (opt-out formats set, own write not captured, cleared after the delay, a newer copy left alone). Not yet checked that Win+V history and cloud sync really leave the item out. Nothing calls it yet (the window is not built) |

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
- **A secret you copy in another app is cleared from the clipboard after about 30 seconds, and removed from Win+V history.** When the app captures something it is sure is a secret, it empties the clipboard 30 seconds later, so you can still paste it once. It only does this if nothing was copied since (checked with Windows' clipboard change counter), so a newer copy is never wiped. Marking an item secret while it is still on the clipboard (the button or `Ctrl+Alt+S`) does the same; marking it normal cancels it. "Review?" items are not cleared, so a false alarm never wipes normal text. If Windows clipboard history (Win+V) is on, Windows records the secret too, so the app also deletes the matching Win+V entries (pinned ones included): about 32 seconds after the copy for the item on the clipboard (deleting its entry also empties the clipboard, so it waits for the paste window), or at once when you mark an older item secret. Marking the item normal cancels this; "Review?" items are never removed. Matching uses the app's keyed hash, so the secret's text is not passed around, but every text entry in Win+V is read into memory while it is checked. Limits: between the copy and the removal the secret sits in Win+V; whether removing it also removes it from other devices that cloud clipboard sync already reached is not verified; and items saved by a very early version of the app (before the keyed hash was stored) cannot be matched.
- **Copy-back of a secret is cleared after about 30 seconds, only if it is still on the clipboard.** "Still there" means this app's window still owns the clipboard: any other app that copies takes ownership, so a newer copy is never wiped. An app that adds data without emptying the clipboard first (unusual) would be cleared along with the secret. If the clipboard is busy, the clear is retried for a few seconds and then given up. Quitting the app clears a pending secret at once. During those 30 seconds, a clipboard manager that ignores the opt-out formats can still record it. Unsure items are copied back the same way.
- **A revealed item is ordinary window text while it is revealed.** Screen readers and other accessibility or automation tools running under your account can read it, like any other window's text. Masked items expose only the bullets. Programs under your account are already outside what this app can protect against.
- **The app's windows are kept out of screen capture, cooperatively.** The app asks Windows to leave every window it opens (the search window and its tooltips, menus, and dialogs) out of screenshots, recordings, and screen sharing, so opening it during a call does not show your history. This works through Windows' own capture paths only: it does nothing against a phone camera, and a program under your account could undo it. It also hides the window from your own screenshots; set `EXCLUDE_FROM_SCREEN_CAPTURE = False` in `config.py` to take them.
- **The app's own clipboard writes are recognised by the clipboard owner.** If the clipboard is owned by one of this app's windows, the change is not captured. This includes copying text inside the app's own window.

## Privacy rules the code follows

- **No network access.** No cloud APIs, telemetry, update checks, or analytics. What has been checked, and what has not, is in [Network](#network).
- **Clipboard content is never logged.** Logs may contain IDs, content types, lengths, and timestamps only. This includes error messages and crash output. The logging module enforces this: it refuses free-text values, discards ordinary logging calls, and records only the class name of an exception, never its message. It cannot stop a stray `print()` or control what third-party libraries print.
- **Only app names are stored as context,** never window titles.
- **Tests use fake data only.**
- **Pause mode** stops capture entirely, with a visible indicator.

## Network

The app is meant to make no network calls at all: no cloud APIs, telemetry, update checks, or analytics. This is what has been checked, and how.

**Automated checks** (`tests/test_no_network.py`, run with the rest of the tests):

1. **Imports.** The test reads every source file of the app and fails if one imports a networking module (`socket`, `ssl`, `http`, `urllib`, `requests`, Qt's `QtNetwork` and web modules, and others).
2. **Running the app's flows.** The test starts a fresh Python process (`tests/no_network_probe.py`) that installs a Python audit hook first, then imports every app module and runs the app's flows with fake text: the clipboard listener started and stopped with capture paused, file logging, the DPAPI-protected key and encrypted database in a temporary folder, capture, duplicate merging, copy-back requests, mark secret and normal, pin, delete, cleanup, the search window drawn off-screen, and Win+V removal through the real Windows Runtime code path (with the history listing replaced, so your real Win+V history is not read). The test fails if Python looks up a host name, uses `urllib`, `http.client`, or similar, or binds or connects a socket to any address other than this machine's loopback address. It also fails if an unexpected networking module is loaded.

What the second check found:

- **Loopback sockets inside the process.** Every Win+V removal opens and closes a pair of connected sockets on `127.0.0.1`. They come from Python's `asyncio`, which `winhistory.py` uses to wait for the Windows Runtime calls: on Windows, `asyncio` makes this socket pair as its internal wake-up signal. The traffic stays inside the machine and is between the app and itself. No other socket is opened.
- **Loaded but not used.** `socket` and `selectors` are loaded by Python's own `logging.handlers` (used for the log file); `asyncio` and `ssl` are loaded by `asyncio` itself (imported by `winhistory.py` and the `winrt` packages). Nothing in the app calls them for networking. No HTTP, URL, or Qt networking module is loaded.

Limits of the automated checks:

- The import check reads import statements only; an import built at run time (`importlib`) would not be caught.
- The audit hook sees only network use that goes through Python. Code written in C or C++ that talks to the network directly (inside Qt, the Windows Runtime, `pywin32`, or Windows itself) is invisible to it. One known case of the last kind: on a PC joined to a work domain, Windows' DPAPI may contact the domain controller to back up its keys. That is Windows, not this app, and it does not send clipboard content.
- The flows run with fakes in place of the real clipboard writes, and real hotkey presses are not covered.

So these checks guard against mistakes; they are not proof.

**Manual check on a real machine:** done on the owner's Windows 11 machine on 2026-11-19, with fake text and a throwaway data folder, following the steps below. Both of the app's processes were polled every second for 3 minutes with `Get-NetTCPConnection` and `Get-NetUDPEndpoint` while fake text was copied (including a fake token, so the clipboard clear and Win+V removal ran), and the window was used to search, re-copy, and mark items. No TCP connection or UDP endpoint was seen at any point. Limits: polling once a second can miss a connection that lasts less than a second (the loopback pair above is one), so this shows there was no lasting connection, not that no packet was ever sent; it covered one session of a few minutes.

### Checking it yourself

Use fake text only, and a throwaway data folder so nothing lands in your real history. In PowerShell, from the repository folder:

1. Start the app with a throwaway data folder:

   ```powershell
   $env:LOCALAPPDATA = "$env:TEMP\smartclip-netcheck"
   .\.venv\Scripts\python.exe -m smartclip
   ```

2. In a second PowerShell window, find the app's processes. The virtual environment's `python.exe` starts a second Python process, so expect two:

   ```powershell
   $ids = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*smartclip*').ProcessId
   $ids
   ```

3. Watch their network connections for two minutes (one line printed per connection found):

   ```powershell
   1..120 | ForEach-Object { Get-NetTCPConnection -OwningProcess $ids -ErrorAction SilentlyContinue; Get-NetUDPEndpoint -OwningProcess $ids -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
   ```

4. While it runs, use the app with fake text: resume capture if it is paused, copy a few fake lines from Notepad, copy a fake token such as `ghp_` followed by 36 `F` characters (this triggers the clear and the Win+V removal about 30 seconds later), open the window with `Ctrl+Alt+V`, search, re-copy an item, and mark one secret.
5. Optionally, open Resource Monitor (`resmon`), go to the Network tab, tick the `python.exe` processes from step 2, and watch "Network Activity" and "TCP Connections" while repeating step 4.
6. Quit the app with the window's **Quit** button, and delete the `smartclip-netcheck` folder in `%TEMP%`.

Expected: no connections at all, except possibly a short-lived `127.0.0.1` pair during a Win+V removal (it lasts milliseconds, so it is rarely seen). Any remote address, or anything listening on `0.0.0.0` or `::`, is a bug.

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

## Labeled-data log (opt-in, off by default)

Phase 2 needs a private test set of real copies with the right labels. If you turn this log on, the app keeps one for you. It is off unless you set `LABELED_LOG_ENABLED = True` in `src/smartclip/config.py` (the settings screen will switch it later).

- **Where:** `%LOCALAPPDATA%\SmartClipboard\labeled.db`, outside the repository, encrypted with DPAPI like the history. Only each record's number is readable in the file.
- **What is written:** one record for each new item captured (a re-copy that merges into an existing item is not written again), and one each time you mark an item secret or normal. A record holds the text, the label, the secret state, whether you confirmed it, the source app's name, the item number, and the time. Never a window title.
- **Secrets are redacted.** Secret and "Review?" items are written only in redacted form: same length, spaces kept, known format prefixes such as `ghp_`, `sk-`, `AKIA` and private-key header lines kept, and every other letter and digit replaced with a random one of the same kind (upper case, lower case, digit). In a one-word item, such as a lone password or token, every symbol is replaced with a random symbol too. In text with several words (JSON, a command, an env var line) punctuation is kept, because there it carries the structure. The real text is never written.
- **Mark normal writes the real text.** Marking an item normal tells the app it is not a secret, so that record holds the text as it is. If you mark a real secret normal by mistake, its text stays in the log until you delete the file.
- **Records are kept until you delete the file.** Deleting an item in the window, or the item expiring, leaves its records in place. That is the purpose of a test set, and the reason the log is opt-in. To stop and erase it, set the option back to `False` and delete `labeled.db`.
- **What redaction still shows:** the length, and where the upper case letters, lower case letters, digits, and symbols are. In text with several words it also shows the exact spaces and punctuation, so a symbols-only word inside such text is written as it is. Field names inside a redacted JSON blob or env var line are randomised along with the rest.
- The log is private and never published. Phase 2 will use it on this machine only.

## Default hotkeys

| Action | Keys |
|---|---|
| Open the search window | `Ctrl+Alt+V` |
| Pause or resume capture | `Ctrl+Alt+P` |
| Mark the last item as secret | `Ctrl+Alt+S` |
| Force-save what is on the clipboard now | `Ctrl+Alt+F` |

These are configurable. They have not yet been tested for clashes with other apps; if another app already holds a combination, it is reported instead of taken over.

- **Mark as secret** always marks secret, never back to normal, so a second press cannot undo a detection. It applies only while the clipboard still holds that item. Marking an item normal is done in the window ("Mark normal").
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

Dependencies are pinned: `pywin32` for the Windows APIs, `PySide6` for the window, the `winrt-*` packages (pywinrt) only to remove secrets from Win+V history, and `pytest` for development. None of them is used to make network calls.

## Development setup

In PowerShell, from the repository folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

Add `ui` to the extras (`".[dev,ui]"`) to install PySide6 as well. It is needed to run the app and the window tests (they are skipped without it).

To run the app:

```powershell
.\.venv\Scripts\python.exe -m smartclip
```

The window opens at start. Closing it only hides it; `Ctrl+Alt+V` opens it again and its **Quit** button stops the app. Starting a second copy shows a message and exits. If the duplicate-detection key file cannot be read, the app does not start and tells you which file it is; it never replaces the file itself. There is no tray icon yet, so the pause state is only shown in the window.

## Using the window

- Type to search. Every word must appear in the item's text or source app. Up and Down move the selection, Enter copies the selected item back, Esc clears the search or hides the window.
- Click a type badge (or the "Secret" / "Review?" badge) to show only those items; click the filter's "x" to clear it.
- Secret and unsure items show as bullets. They are never matched by a search, and while you type a search they are left out of the results. Click the bullets (or press Ctrl+R) to reveal one for 10 seconds; it is masked again when the time is up or the window hides.
- Copying a secret or unsure item back uses the safe copy-back (kept out of Win+V where honored, cleared after 30 seconds) and does not reveal it. The window hides after a copy.
- The window hides when you switch to another app, and every revealed item is masked again.
- Ctrl+P pins or unpins a normal item. Shift+Del deletes the selected item after asking.

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
