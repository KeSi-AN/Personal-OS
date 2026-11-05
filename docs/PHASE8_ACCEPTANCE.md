# Phase 8 acceptance: the voice popup

**Asked for:** a hotkey-triggered voice popup using pywebview. It runs in a separate process, launched on demand, and data reaches it by files, not a socket. It shows the live state, the transcript and the reply, has pin, Esc and dashboard controls, and closes by itself. It must cost nothing while closed, and must never affect capture or voice.

**Run:** 2026-11-06 on this laptop. No captured content is reproduced here, because this repo is public; the demo uses fake data.

## Result: built and checked; awaiting your try with a real turn

| Requirement | How it's met | Checked by |
|---|---|---|
| Separate process, on demand | The voice process starts `personal_os.interfaces.popup` (windowless) on a background thread when a turn starts (hotkey, wake word, barge-in). Recording doesn't wait for it. | `test_a_turn_is_published_live_and_old_fields_stay` |
| One at a time | `runtime/popup.lock`. The voice process probes it before starting a popup, and a second popup process gives up quietly. | `test_the_lock_allows_one_popup`, `test_second_popup_process_exits_without_a_window`; by hand: the second `voice popup` says *already open* |
| A crash never reaches voice | Launch errors are caught and kept in `runtime/popup.json` for doctor. A status file that can't be written no longer stops a turn. | `test_a_popup_failure_never_reaches_voice` |
| pywebview + WebView2, no Node, no build step, nothing from the internet | The page is an HTML string in the package, so pywebview's server never starts and no port opens. A CSP of `default-src 'none'` (inline script and style only) blocks every request. | `test_page_is_local_only` |
| No WebView2 or pywebview: no popup, no crash | `launch` checks both first, records why, and returns. doctor's `popup` line shows it and adds a problem. | `test_missing_pywebview_or_webview2_falls_back` (both cases) |
| Data by files | The popup polls `voice.json` and the pause and kill-switch markers every 220 ms through `popup_view.read_view`, a pure function. | `test_view_from_status`, `test_read_view_shows_pause_and_kill_switch` |
| Live turn in the status file | `voice.json` gains `turn`: `id`, `trigger`, `state` (listening, thinking, speaking, done), `heard` (as soon as speech is recognised), `reply`, `question`, `started_at`, `done_at`, `error`. The top-level fields, `voice status`, `voice log` and the dashboard card are unchanged. | `test_a_turn_is_published_live_and_old_fields_stay` |
| Atomic writes | `runtime.write_json_atomic`, now retrying for about 100 ms when Windows refuses the swap because a reader has the file open (which the popup's polling makes likely). | `test_atomic_write_retries_while_a_reader_holds_the_file`, `test_atomic_writes_with_a_real_reader_polling` (300 writes against a live reader, no torn read) |
| UI | A pulsing dot, a spinner and speaker bars for the states. Transcript, reply, "Did you mean …?", paused and kill-switch notices. Pin, Dashboard (one-time login link), ✕, and Esc. Dark theme with the dashboard's colours. Frameless, on top, opened without focus in the configured corner. | `voice popup --demo`, screenshots taken during the run (not committed) |
| Auto-close and reuse | Closes 5 s after the turn is done. Pinned, it stays open; unpinning restarts the delay. A new turn clears the same window and restarts the timer. | `test_autoclose_after_the_reply`, `test_pinned_never_closes_and_unpinning_restarts_the_delay`, `test_a_new_turn_reuses_the_window_and_restarts`, `test_voice_stopping_closes_it_too` |
| Privacy | It shows only `voice.json` fields (what voice already keeps), nothing captured, and writes no transcript anywhere new. `popup.json` holds only times and error names. | Code review; `View` has a fixed set of fields (`test_view_from_status`) |
| Budget | About 145 MB private memory and 1.7% CPU while open and animating. Renderer up 1.5 s after the spawn. Every process exits on close. | `scripts/measure_budget.py --popup 60`, [BUDGET.md](BUDGET.md#voice-popup-phase-8) |
| Config, CLI, doctor | `[voice] popup`, `popup_position`, `popup_autoclose_s`. `personal-os voice popup [--demo]`. doctor: `popup on, WebView2 <version>`, plus the last error. | `test_doctor_when_the_popup_is_off`, the fallback tests, by hand |

Checks: `uv run pytest -q` passes (527 tests), and `ruff check .` and `mypy personal_os` are clean.

## Not yet tried

**A real push-to-talk turn with the popup.** I didn't press the hotkey myself, because it would record whatever was being said near the laptop. Everything around it is tested, and the demo shows every state in the real window.

## Things to know

- **Esc needs a click first.** The popup opens without taking focus, so it never steals keystrokes from what you're typing. Clicking it gives it focus, and then Esc closes it.
- **Startup:** the window appears about 1.5 s after the hotkey, while you're still speaking. Showing it sooner would mean keeping a hidden window running, which would break "zero cost while closed".
- **While open,** the CPU goes to WebView2 drawing the animations. Polling the files costs about 0.15%. A real turn keeps it open only for the turn plus 5 s.
