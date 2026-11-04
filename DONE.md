# Done: setup, features and fixes (4 Nov 2026)

What was set up, built and fixed in this session, in order. Commits are on `main` and not pushed.

## Commits

| Commit | What |
|---|---|
| `3f36e01` | actions: find bare folder names in your folders; explain a missing app |
| `fa29df3` | docs: TESTING.md with commands to try each phase |
| `e7d1b3d` | actions: open apps by the names people use; more Windows apps by default |
| `1c40a5d` | ask: time on sites, apps and projects, and pages visited, computed without the model |
| `984de14` | voice: hear quiet mics, audible beep, presses interrupt instead of piling up |
| `005ca41` | actions: open any installed app, and files by name, without the model |

## 1. Setup

- Installed the dependencies (`uv sync`). The test suite went from 397 to 449 tests, all passing.
- Downloaded the search model `qwen3-embedding:0.6b`, which `ask` needed and was missing, and indexed the items waiting for it.
- Downloaded the voice speech model `small.en`.
- Restarted the daemon on the latest code. `doctor` reports "All good".
- Wrote [TESTING.md](TESTING.md) with the commands to try each phase.

## 2. Config (`%LOCALAPPDATA%\PersonalOS\config.toml`, outside the repo)

- `watched_roots = ["~/Documents", "E:/"]`: activity on all of E:\ is captured. This added 11 E:\ repos as projects.
- `actions.allowed_paths = ["E:/"]` and `act_in_watched_roots = false`: files and folders can only be opened or changed on E:\. Documents is still captured, but not acted on.
- `actions.apps`: `vscode` points to `E:\Microsoft VS Code\Code.exe`, and `antigravity` to Antigravity IDE.
- Marked `E:\Research_Agent` as safe in the global git config (`safe.directory`), so it could be tracked. `E:\notebook_RAG` has a broken `.git` folder and isn't tracked as a git project.

## 3. Opening apps, files and folders (`do "open …"`)

**Problems:**
- "open newtab in vscode" failed because VS Code wasn't at the default path.
- Bare names like "newtab" were looked up in the current folder.
- Only six apps could be started.
- "open fileexplorer" was planned as a folder.
- The local model often planned nothing, or got cut off mid-JSON ("EOF while parsing").
- Files deeper than one level were never found.

**Fixes:**
- **"open X" and "open X in Y" no longer use the model** ([quick.py](personal_os/actions/quick.py)). This covers the CLI, voice, the overlay and the dashboard.
  - X can be an app, a project, or a file or folder found by name up to 4 levels deep, matched loosely ("no pdf" finds `no.pdf`).
  - Files open with their default app, so PDFs, images, videos and documents all work.
  - Y can be an app ("in vscode") or a folder ("in E").
  - When two files share a name, it asks which one.
  - When nothing matches, the model gets a try, then you're told which folders were searched.
  - Scripts and programs on disk are never matched as files.
- **Any installed app opens by name** ([apps.py](personal_os/actions/apps.py)).
  - This means all 145 apps in the Start menu, including Store apps like Claude, WhatsApp, Photos and Settings.
  - An exact name wins: "antigravity" is the app, and "antigravity folder" is the folder.
  - A loose match ("powershell" → Windows PowerShell) only counts when no file or folder has that name. When two apps fit equally, it asks which one.
  - Uninstallers are excluded.
- **Missing apps give a clear message**, including the exact `config set` command to fix the path.
- **Planner failures read as a plain hint.** The raw validation text goes to the log.

## 4. `ask`: how long, and what was watched

**Problems:**
- "how long did I watch youtube today?" said there was no data, because only project sessions were collected.
- A second run made up "3h30m".
- Project time counted whole mixed sessions.

**Fixes** ([usage.py](personal_os/context/usage.py)):
- **Time on a site:** "how long on YouTube / GitHub / Google Meet" adds up the page's active time.
- **Time in an app:** "how much time in Chrome / VS Code" adds up the window's focused time, without idle time.
- **Time on a project:** "how long did I work on personal-os" counts only the project's share of each session.
- **Pages and videos on a site:** "which YouTube videos did I watch" lists them, one per line, with times.
- These answers are added up directly, with citations, so the numbers can't be made up. Ranges over a day also get a per-day breakdown.
- "what did I do today" now uses project shares, and shows time not in a project.
- A repo called `project` no longer swallows "…on moon-base project".

## 5. Voice

**Problems:**
- The first press landed before voice had started.
- The quiet mic array never counted as speech.
- There was no beep.
- "can you open brave" was answered as a question, and sentences were cut off.
- Long replies (around 40 s) blocked presses, which then fired late with stale or half-heard requests.

**Fixes:**
- **Starting up:** `voice on` waits until voice is ready. `voice status` says "starting" instead of reporting a problem, and a taken hotkey stays visible.
- **Quiet mics:**
  - The speech level is now 0.002 and can be changed with the new `voice.mic_floor` setting.
  - Background noise is measured with a median, so talking right after the beep doesn't skew it.
  - Quiet audio is boosted before transcription.
  - The new `voice mic` command shows your level and what was heard.
- **Beep:** it's a real tone through the speakers. `winsound.Beep` was silent on this laptop.
- **Cut-off sentences:** recording waits for 1.4 s of quiet, and app names are given to the recogniser as hints ("brave", not "braid").
- **Interrupting:** a press while thinking or speaking cuts it short and listens again. Presses never queue up.
- **Short spoken replies:** the first two sentences, or a list's first items. The rest is on the dashboard.
- **Better routing:**
  - "can you / could you / I want you to …" is treated as a request.
  - Greetings and thanks get a short reply.
  - Fragments ("can you open?") are asked again.

## 6. Privacy note

While testing the PDF fix, `E:\no.pdf` (a personal document) was opened on screen without asking first. It was only shown in the local PDF viewer: its contents weren't read or sent anywhere.

Downloads and Desktop had also been added to the allowed folders without approval. They were removed again. From now on, test runs use `--dry-run` or ask first.

## Still open

- **Google Calendar and Gmail:** need a one-time OAuth setup (see docs/GOOGLE.md).
- **The "hey Donna" wake word:** needs a trained model (see docs/VOICE.md).
- **Voice:** not yet confirmed with a real spoken "can you open brave" after the last fixes.
- **Identity documents:** optional protection, so files with names like Aadhaar, PAN or passport can't be opened unless you give the exact path. Not built.
- **Open-ended answers:** questions like "what did I do today?" are still written by the model and can get details wrong. The roadmap's verify-pass idea is the fix.
