# Phase 1 acceptance

**Brief's test:** "Run a real 1-hour session; events are accurate; CPU/memory budget met."
**Run:** 2026-11-03, 18:30–18:57 (about 27 min; you stopped it early), on the real data dir. Reviewed with `scripts/review_capture.py`. No captured content is reproduced here, because this repo is public.

## Result: accepted, with two fixes made during review

| Check | Result |
|---|---|
| Budget: CPU < 2%, memory < 300 MB | **Pass.** 0.065% average CPU, 53 MB peak ([BUDGET.md](BUDGET.md)). |
| Window focus intervals match what was used | **Pass.** 82 intervals in the right order across the browser, VS Code, Explorer, terminal and the video call. Quick Alt-Tabs are folded into the surrounding interval and counted, not split out. VS Code file switches show as title changes. |
| Terminal commands | **Pass after fix 1.** Command, duration, exit code, repo and branch were all correct. |
| Git: commits, pull/merge | **Pass.** One local commit plus the 4 commits brought in by a pull (including the merge commit). 26 repos baselined. |
| File edits with line counts | **Pass after fix 2.** Edits, creations and line counts were correct; the hook install showed up as the profile file being created. |
| Pause / resume (CLI) | **Pass.** Markers recorded, nothing captured during the pause, open intervals closed at the pause time. |
| Clean stop/start | **Pass.** No intervals needed crash recovery. |
| Privacy blocks | Nothing blocked came up in this run (no WhatsApp/Instagram/incognito). Covered by the simulator and unit tests. |
| Bus drops / filter errors / observer restarts | None. |

## Fixes made from the review

1. **Editor bootstrap commands.** VS Code injects a shell-integration command into every new integrated terminal, and it was being recorded as if you'd typed it. It's now ignored (`is_tool_bootstrap`), along with Cursor's equivalent.
2. **File changes made by git.** The files rewritten by a `git pull` looked like 11 hand edits (+151 lines and so on). Changes made in a repo within ±10 s of a worktree-rewriting git operation (pull, merge, checkout, rebase, cherry-pick, revert) now carry `via_git: true`. Commits don't trigger this, so your own edits just before a commit aren't affected.

## Not exercised in this run (covered by tests only)

- Idle longer than 5 minutes, screen lock (Win+L), sleep/wake.
- The global pause hotkey (Ctrl+Alt+Shift+P).
- The Git Bash hook (only PowerShell was installed).
- `forget` on real data (deliberately not run on your data; the daemon test and the file-byte scan cover it).

These can be spot-checked at any time with `uv run python scripts/review_capture.py --minutes 30`.
