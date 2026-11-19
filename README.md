# Smart Clipboard

A local, privacy-focused clipboard history for Windows. It remembers what you copy, labels each item (link, code, JSON, possible secret, and so on), keeps everything encrypted on your own machine, and lets you search and paste from a small Win+V-style window.

**It is not a password manager.** Don't store real passwords in it; use Bitwarden, KeePassXC, or similar. Its value is automatic sorting and labeling of everyday clipboard text, and treating anything that looks like a secret carefully.

## Why

Windows' built-in clipboard history (Win+V) keeps about 25 items, has no real search or labels, and loses everything on restart unless items are pinned. Other clipboard managers exist and cover much of this; this project doesn't claim to beat them. What it aims to offer:

- A system-wide Windows watcher with labels that use the source app as a hint
- Careful handling of secrets: masked, kept for 2 hours only, cleared from the clipboard and Win+V history
- An honest threat model (below), and a measured evaluation of its detection in Phase 2

## Features

- **Text history that survives restarts**, encrypted with Windows DPAPI in a local SQLite file
- **Labels by rules:** Link, Code, JSON, Shell, Path, Env, Error, SQL, IP, Hash, Address, Note
- **Secret detection** with three outcomes: normal, secret (masked, deleted after 2 hours), or unsure ("Review?", treated like a secret until you decide)
- **Privacy controls:** pause, an app exclusion list, and respect for the Windows flags password managers set to keep data out of clipboard tools
- **Safe paste-back of secrets:** kept out of Win+V and cloud sync where Windows honors it, and cleared from the clipboard after 30 seconds
- **Duplicates merge**; normal items are kept 90 days after their last copy; normal items can be pinned
- **No network access at all**

## Status

Phase 1 is built and tested (680+ automated tests, all with fake data) and is now in a week of daily use on Windows 11. Checked by hand so far: the clipboard clear and Win+V removal of secrets, the no-network check, and starting and stopping the app. Not yet checked by hand: pasting from the new window into real apps, real password managers, and how often the source app is named correctly. Detection rules are starting values, not measured ones; Phase 2 measures them.

## Install and run

Requires Windows 10 or 11 (developed on 11) and Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ui]"
.\.venv\Scripts\python.exe -m smartclip
```

The window opens at start. Closing it only hides it; quit from the "..." menu at the top of the window. Only one copy runs at a time. There is no tray icon or autostart yet.

## Using it

| Keys | Action |
|---|---|
| `Ctrl+Alt+V` | Open the window next to the mouse pointer |
| `Ctrl+Alt+P` | Pause or resume capture |
| `Ctrl+Alt+S` | Mark what is on the clipboard now as secret |
| `Ctrl+Alt+F` | Save what is on the clipboard now, even while paused |

In the window:

- **Paste:** click a card, or use Up/Down and Enter. The item is pasted into the app you were using, but only if that same app is back in front and no modifier key is held; otherwise it is just copied and you press Ctrl+V.
- **Search** by typing; **filter** with the chips under the search box or a card's badge. Esc clears the search, then hides the window.
- **Secrets** show as bullets and never match a search. Click the bullets (or Ctrl+R) to reveal one for 10 seconds.
- **More:** the "..." on a card, or a right-click, opens Paste, Copy only, Reveal, Pin, Mark normal, Mark secret, and Delete.

The window follows your Windows light or dark setting, hides when you switch apps, and is kept out of screenshots and screen sharing.

## Privacy and threat model

Everything stays on your machine. Each item is one encrypted payload; only its row number and creation time are readable in the database file. Duplicates are found with a keyed hash (HMAC-SHA256 with a DPAPI-protected key), so short secrets can't be brute-forced from the file. Logs hold only IDs, types, lengths, and counts, never clipboard text.

| DPAPI protects against | DPAPI does not protect against |
|---|---|
| Someone copying the database file | Malware running under your Windows account |
| Another user account on the same PC | Anyone logged in as you |
| Casual snooping at the disk level | Someone who knows your Windows password |

Limits to know:

- **Data is tied to your Windows account.** A profile reset or a move to another PC makes it unreadable, with no recovery.
- **Some metadata is visible:** how many items exist, when each was first saved, and roughly how long each is.
- **Secrets can't be wiped from memory.** While the app runs, decrypted items are in Python's memory, and a revealed item is ordinary window text that accessibility tools can read.
- **The Windows opt-out flags are cooperative.** This app honors them and sets them on secrets it pastes back, but another clipboard tool is free to ignore them.
- **Password managers are skipped only if they set those flags, or if you exclude them.** The exclusion list starts empty.
- **Secrets copied elsewhere** are cleared from the clipboard after 30 seconds (only if nothing newer was copied) and removed from Win+V history shortly after. Whether that removal reaches devices that cloud clipboard sync already reached is not verified.
- **Detection makes mistakes both ways.** Hashes, IDs, and file names look like passwords; passphrases of plain words are missed. "Review?" exists for the uncertain middle.

## No network

The app makes no network calls: no cloud, telemetry, updates, or analytics. Two automated tests check this: app code imports no networking module, and the app's flows run with an audit hook that fails on any lookup or any socket outside the machine. The only sockets are a loopback pair Python's `asyncio` makes internally. A manual check of the running app on Windows 11 saw no connections. Code inside Qt, the Windows Runtime, or Windows itself is outside what the tests can see.

To check it yourself, start the app with a throwaway data folder, find its two `python.exe` processes, and watch them while you copy and paste fake text:

```powershell
$ids = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*smartclip*').ProcessId
Get-NetTCPConnection -OwningProcess $ids -ErrorAction SilentlyContinue; Get-NetUDPEndpoint -OwningProcess $ids -ErrorAction SilentlyContinue
```

Nothing should be listed, apart from a brief `127.0.0.1` pair. Resource Monitor's Network tab shows the same.

## Configuration

Settings live in `src/smartclip/config.py` until there is a settings screen:

| Setting | Default |
|---|---|
| `NORMAL_RETENTION` / `SECRET_RETENTION` | 90 days / 2 hours |
| `EXCLUDED_APPS` | empty (exe names in lower case; check the real name in Task Manager's Details tab) |
| `PASTE_ON_CHOOSE` | `True` (`False` = choosing only copies) |
| `EXCLUDE_FROM_SCREEN_CAPTURE` | `True` |
| `REMOVE_SECRETS_FROM_WIN_HISTORY` | `True` |
| `LABELED_LOG_ENABLED` | `False` (see below) |
| Hotkeys | as in the table above |

Data lives in `%LOCALAPPDATA%\SmartClipboard\`, never in the repository.

**Labeled-data log (opt-in).** With `LABELED_LOG_ENABLED = True`, the app keeps a private, DPAPI-encrypted record of what it labeled and how you corrected it, as a test set for Phase 2. Secret and "Review?" items are only ever written redacted: same length and shape, every letter and digit replaced at random, known prefixes such as `ghp_` kept. Records stay until you delete `labeled.db`.

## Phase 2: measured detection

Phase 1 detects secrets and content types with hand-written rules. Phase 2 trains small local models (run on the CPU, no cloud) and compares them honestly against those rules on a private test set from daily use plus synthetic data, reporting recall, false alarms, and sample counts. Whatever wins (rules, a model, or a hybrid) ships in the app; if the rules win, the write-up will say so. Real clipboard data used for evaluation stays private. A later part extends the app beyond text to images, files, and PDFs.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use fake data only. A few opt-in tests touch the real clipboard and replace its contents; they run only with `SMARTCLIP_REAL_CLIPBOARD_TESTS=1`.

## License

MIT. See [LICENSE](LICENSE).
