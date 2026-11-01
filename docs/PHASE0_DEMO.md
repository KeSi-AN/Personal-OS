# Phase 0 demo

Phase 0 has no live capture yet. Everything below runs on **simulated** events, fed through the real bus → privacy filter → SQLite pipeline. Nothing here observes your machine.

All commands run from the repo root in PowerShell. To keep the demo out of your real data dir, point it at a scratch folder first:

```powershell
$env:PERSONAL_OS_HOME = "$env:TEMP\personal-os-demo"
```

## 1. Run the tests

```powershell
uv run pytest -q
```

Expect `127 passed`. The tests include the golden secret corpus, property tests, the forget byte scan, and the end-to-end simulated day.

## 2. Simulate a morning of work

```powershell
uv run personal-os sim run tests/fixtures/scenarios/workday.toml --fresh
```

The scenario ([workday.toml](../tests/fixtures/scenarios/workday.toml)) contains coding, a failing test, Stack Overflow research, a commit, idle time and a screen lock. It also contains things that must **not** be stored: WhatsApp (app and web), Instagram, an incognito tab, a login page, a 2-second bounce, a `.env` edit, a Gmail tab, and several planted secrets (a Bearer token, an API key in a URL, a GitHub token, `mysql -phunter2`).

The output lists what was blocked (and why), how many events were marked sensitive, and which redactors fired.

## 3. Look at what was stored

```powershell
uv run personal-os events --db "$env:PERSONAL_OS_HOME\sim.db" --last 60
```

Things to check:

- Each focus interval is **one row**, even though the simulator sent a checkpoint every 60 s.
- Terminal commands show `[redacted]` where the secrets were.
- WhatsApp and Instagram tabs appear only as `window.focus [redacted]` (time spent, no title). The WhatsApp desktop app doesn't appear at all.
- The Gmail visit is `[S]` (sensitive): origin and time only, no title or page text.
- The `.env` change is `[S]` with no size or line counts.
- There's no incognito, login-page, or bounce visit.

## 4. Search it

```powershell
uv run personal-os search "refresh token" --db "$env:PERSONAL_OS_HOME\sim.db"
uv run personal-os search "auth_flow" --db "$env:PERSONAL_OS_HOME\sim.db"
```

The second query matches only text inside a stored page extract (the HTTPX docs page).

## 5. Audit what's stored

```powershell
uv run personal-os inspect --db "$env:PERSONAL_OS_HOME\sim.db"
```

Shows rows and bytes per source and privacy class, plus the daily counters for blocked, sensitive, and redacted events. Counters hold numbers only, never content.

## 6. Prove the secrets never touched the disk

```powershell
Select-String -Path "$env:PERSONAL_OS_HOME\sim.db*" -Pattern "hunter2|ZQXJTOKEN|482913"
```

Expect no output.

## 7. Forget a window

```powershell
uv run personal-os forget --since 2026-11-02T09:20:00+05:30 --until 2026-11-02T09:30:00+05:30 --db "$env:PERSONAL_OS_HOME\sim.db"
uv run personal-os search "auth_flow" --db "$env:PERSONAL_OS_HOME\sim.db"
```

It asks for confirmation, deletes everything overlapping the window (including page text and full-text index entries), and leaves a `meta.forget` marker. The search then returns `No matches.`

## 8. Pause and resume

```powershell
uv run personal-os pause --for 30m
uv run personal-os status
uv run personal-os resume
```

In phase 0 this only sets the persisted flag that the filter honours. From phase 1 the observers and shell hooks respect it too.

## Clean up

```powershell
Remove-Item -Recurse -Force "$env:PERSONAL_OS_HOME"; Remove-Item Env:PERSONAL_OS_HOME
```
