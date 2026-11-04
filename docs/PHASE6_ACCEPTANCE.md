# Phase 6 acceptance

**Brief's test:** "Everything controllable and inspectable without the terminal."

**Run:** 2026-11-04, live on this laptop (dashboard in a browser, tray, hotkeys, phone listener tested from the laptop) and in tests. No captured content is reproduced here, because this repo is public.

## Result: target met, awaiting your review

| Control or view | Where, without the terminal |
|---|---|
| Is capture running, paused, actions off | Tray icon colour and menu; every dashboard page's header |
| Pause, resume, forget the last N minutes | Tray (pause), Today, Privacy, phone |
| Turn each source on/off, then apply | Privacy → Sources → Restart capture (restarts on its own: ADR-062) |
| First-run review of what's captured | Welcome card on Today → Privacy → Sources, each with a plain description |
| Retention, blocked sites and apps | Privacy |
| What's stored | Privacy → What's stored (per source and kind) |
| Export everything / one project | Privacy, a project's page (zip of JSON or Markdown) |
| Delete one project's data | A project's page (type its id to confirm) |
| Sessions, summaries, the evidence behind them | Today, Timeline |
| Project current state | Projects → a project |
| Ask, with follow-ups | Ask page, the overlay, phone |
| Assign unassigned items | Inbox |
| Do things; approve steps that need it | Actions (Approve / Decline cards), the overlay (Yes / No) |
| Kill switch, tier 3 unlock and lock, undo | Tray, Ctrl+Alt+Shift+K, Actions |
| Audit log | Actions |
| What it remembers | Memory (add, edit, forget); "remember that …" in Ask or the overlay |
| Reminders | "remind me …" via Actions or the overlay; fired as tray notifications |
| Phone access: on/off, pairing, revoke | Privacy → Phone |

The terminal is still needed for a few one-time installs: the shell hooks, the browser host, and the firewall rule for phone access, which needs admin rights. `doctor` and `setup` remain as conveniences.

## Live checks

- **Dashboard:** every page rendered in a real browser (Edge-based pane), dark theme included. "where did I leave off on personal-os?" on the Ask page answered from your real data with cited evidence: the volume commands you'd just tried, and the commit after them.
- **Security:**
  - wrong Host → 421;
  - no session → 401;
  - login links work once;
  - cross-site or header-less changes → 403 (tests).
- **Tray:** started with the daemon. `doctor` reports `tray shown`.
- **Hotkeys:** pause and kill registered. The overlay's first choice, Ctrl+Alt+Space, turned out to be **taken by another app**; `doctor` now reports that. Moved to Ctrl+Alt+Shift+Space, and the hotkey opened the overlay (verified by its process starting).
- **Phone listener** (turned on for the test, then off again):
  - TLS 1.3 on the LAN address only; plain HTTP refused;
  - unpaired → pairing page;
  - a 6-digit code paired a test device: Today 200, Privacy 403, pause worked;
  - revoking sent it back to pairing.

  The test device was deleted afterwards.
- **Budget:** with the dashboard server and tray loaded, the daemon used **0.14% CPU over 60 s idle and 77 MB**, against the brief's limits of about 2% and 300 MB.

## Tests

363 pass, 6 deselected. The model tests (`pytest -m llm`) all pass after one fix (below). Lint and types are clean. New:

- `tests/test_web.py`: security, every page, ask with follow-up and remember, pause/forget, inbox, `do` with approval, the settings allow-list, export, project delete, phone pairing and limits.
- `tests/test_phase6.py`: memory, follow-ups, reminders, brightness, rollups (grounding, redo on change, forget), pairing limits, deferred confirmation.

## Problems found on the way

| Found | Fixed |
|---|---|
| The web listener didn't start in the background daemon: uvicorn configures console logging, and the windowless daemon has no console. | uvicorn uses the daemon's logging (`log_config=None`). |
| The Ask page stayed empty: its script ran before the shared helpers were defined. | Shared JavaScript moved into the page head. |
| The overlay hotkey did nothing, silently: another app owns Ctrl+Alt+Space. | Ctrl+Alt+Shift+Space, and `doctor` reports a taken hotkey. |
| Request bodies weren't recognised (models defined inside a function). | Moved to module level. |
| Pairing needed a form parser dependency. | Parsed with the standard library instead. |
| Rerunning the phase 5 model test: with `set_brightness` added, "turn the volume up" was planned as brightness 100%. | A tool can declare cue words; `set_brightness` is only accepted when the request mentions brightness, dimming or the screen, and the model is told why and retries. "dim the screen to 30%" was added to the test. |

Two slips of mine while testing:

- One check script matched its own command line and terminated itself.
- The `daemon stop` / `start` cycles meant your dashboard sessions had to be signed in again.

Neither affected your data.

## Not done, and why

- **Do-not-disturb:** Windows has no public API for Focus Assist.
- **QR codes for pairing:** left out to avoid a dependency; you type the address and code.
- **Phone confirmations of tier 2+ actions:** never, by design.

## Check it yourself

```powershell
uv run personal-os doctor
uv run personal-os dashboard
```
