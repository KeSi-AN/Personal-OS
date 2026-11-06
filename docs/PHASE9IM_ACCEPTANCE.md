# Phases 9i–9m acceptance

**Asked for:**
- the popup shows an answer before Donna speaks it;
- for actions she says what she's doing, then does it;
- the popup shows whole answers instead of "the rest is on the dashboard";
- projects you curate (tabs, tab groups, links, notes, to-dos), to come back to later;
- a new dashboard with time spent, notes, sources and to-dos;
- Donna knows her own settings and your past conversations with her.

**Run:** 2026-11-07, on branch `browser-control`. No captured content is reproduced here.

| Phase | What was built | Checked by |
|---|---|---|
| 9i Show, then say | The popup records which reply it has drawn (`runtime/popup_seen.json`). Voice waits for it before speaking: up to 0.6 s if the popup is open, 2 s while it's starting, not at all without one. Fast commands, browser play/pause and the agent's actions are announced first ("Pausing.", "Playing Circles.", "Opening Brave."), then run. A failure is still said. | `test_actions_are_announced_before_they_run`, `test_fast_actions_say_first_then_act`, `test_voice_waits_for_the_popup_then_speaks` |
| 9j The whole answer on screen | Replies carry `details` (the model's own, or the read results the reply was built from). The popup grows to 480×560 for them, renders lists, makes links clickable (web addresses only) and stays up 20 s. Speech says "the rest is on screen". | `test_read_results_become_on_screen_details`, the popup tests; the big card was seen live in the demo |
| 9k Projects you curate | `project_items` (migration 0007). Tools to create a project, add sources (the current tab, a tab group, matching tabs or a link), save notes, add and tick off to-dos (undoable), list items, file the inbox, and resume. Extension 1.3.0 adds a right-click **Add to Personal OS project**, and a tab group named after a project keeps it in sync. A link teaches the tracker its repo or specific site; never YouTube or Google. Export, forget and delete cover it. | `tests/test_project_items.py` (10 tests) |
| 9l Dashboard v2 | Today, Projects, each project's page, Donna, Notes and Settings. The old home moved to `/old`, and the classic privacy, memory and actions pages are linked from Settings. The phone gets read-only Today and Projects. | `tests/test_dashboard_v2.py` (9 tests); Today, Projects and Donna checked live with your data |
| 9m Donna knows herself | `about_me` (wake word, voice, follow-ups, popup, browser, model, capture, hotkeys) and `conversation` (your recent exchanges with her). The prompt says her earlier replies can be wrong. | `test_about_me_and_conversation_tools`; `about_me` run against your real settings |

Checks: `uv run pytest -q` passes (764 tests), and `ruff check .` and `mypy personal_os` are clean.

## For you to do

- **Reload the extension** in `brave://extensions`. It's version 1.3.0 now and needs the context-menu and tab-groups permissions.
- **Name a Brave tab group exactly like a project** (for example "Personal-OS") to have its tabs kept as that project's sources.

## Not yet tried by voice

The announcement order, the popup wait and the project tools by voice are covered by tests, but haven't been tried by voice yet.
