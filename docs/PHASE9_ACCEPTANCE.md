# Phase 9a acceptance: Donna as one assistant

**Asked for:** an autonomous, completely local assistant. It should keep track of your day and work, do things when asked, and be a proper voice companion. This is the foundation phase of [AGENT.md](AGENT.md). The later phases cover proactive help, skills as data, an MCP client, reading any app, live-feeling voice and a bigger local brain.

**Run:** 2026-11-06 on this laptop: GTX 1650 4 GB, `qwen3:4b` through Ollama. No captured content is reproduced here; answers about your own activity are summarised.

## Result: built and checked live; awaiting your review

| Request (real model) | What Donna did | Time (warm) |
|---|---|---|
| "What's the difference between a process and a thread?" | Answered from her own knowledge, with no tools | about 2 s |
| "Give me three ideas for a short break." | Answered directly | 2.2 s |
| "How many windows do I have open, and which app is in front?" | `now`, then answered from it (correct count and app) | 4.1 s |
| "Do I have any reminders coming up?" | `list_reminders`, then answered | 5.5 s |
| "Remind me in 45 minutes to review the agent test" | `set_reminder`, and said the time and text back | 4.4 s |
| "Cancel the agent test reminder" | `cancel_reminder` (the one matching those words) | 3.1 s |
| "Thanks Donna, you're doing great" | A short reply, no tools | 1.3 s |
| "What did I work on today?" | `recall` (the cited history pipeline), whose answer was passed straight through | 25.8 s |

The first request after the model is unloaded takes about 15–20 s longer while it loads.

**How it got there.** The first version failed on a small model:
- general questions called `now` four times over;
- a checker rule rejected every final answer that mentioned a tool;
- stray arguments made repeated calls look new.

What fixed it:
- the model's first JSON field is now the decision ("answer" or "use_tools");
- the rules and worked examples come after the tool list;
- repeated calls are compared after argument validation;
- the last step can only answer.

Tests cover each of these.

Checks: `uv run pytest -q` passes (690 tests); `ruff check .` and `mypy personal_os` are clean.

## Not yet done

- **Open tabs in Brave need two steps from you:** `personal-os browser install --browser brave` (it registers the connector, which is a registry change, so it's yours to run), then reload the extension in `brave://extensions`. Until then Donna says the tab list isn't known rather than guessing.
- **`recall` is slow** (about 26 s). It's the existing history pipeline, with its own model call and search. Speeding it up belongs with phase 9f/9g.
- **Proactive help, skills, MCP, reading any app, and streaming speech** are the next phases (AGENT.md), each with its own review.
- **The overlay and the new `personal-os chat` use the agent too.** The dashboard's ask and do boxes are unchanged.
