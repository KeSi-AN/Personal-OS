# Roadmap: the rest of the MVP, then what comes after

**The MVP is the brief's phases 0–7.**
- **After the MVP:** the post-MVP backlog below is worked on once those are accepted.
- **Before it ends:** items under "Gaps" are brief requirements that aren't built yet. They're closed before the MVP is called done.

## The MVP

| Phase | What | Status |
|---|---|---|
| 0–2 | Foundation, desktop capture, browser | Accepted 2026-11-03 |
| 3 | Projects, inbox, sessions | Built, awaiting review |
| 4 | Local model: summaries, hybrid search, `ask` | Built, awaiting review |
| 5 | Action layer: tool registry, tiers, confirmation, audit log, kill switch, injection tests | Accepted 2026-11-04 |
| 6 | Tray, hotkey overlay, web dashboard (reachable from your phone), privacy centre | Built, awaiting review |
| 7 | Voice, briefings, MCP/plugin API, Google Calendar and Gmail | Built, awaiting review (Google sign-in and "hey Donna" model wait on you) |

## Gaps: brief requirements still open (close before the MVP is done)

| Requirement (brief section) | Where it lands |
|---|---|
| Export of everything stored, as JSON and Markdown (§5) | **Done** in phase 6 (`export`, Privacy page) |
| Per-project delete (§5) | **Done** in phase 6 (`projects delete`, project page) |
| Conversation memory: short-term chat context, plus long-term facts you confirm ("remember that…"), stored separately and editable (§8) | **Done** in phase 6 (follow-ups, Memory page, `memory`) |
| Per-project timeline: daily and weekly summaries, plus a living "current state" note (§5) | **Done** in phase 6 (day rollups, current-state notes); weekly is still answered at question time |
| First-run wizard that explains each source before enabling it (§9) | **Done** in phase 6 (welcome card, Privacy → Sources, `setup`) |
| Tier 1 tools that need notifications: brightness, do-not-disturb, timers and reminders (§7) | **Done** in phase 6 except do-not-disturb (no Windows API) |
| Optional encryption at rest (§5; deferred by your answer to Q8) | Phase 7 or right after the MVP |
| Embeddings in the project resolver (§6; deferred in phases 3–4) | After the MVP, once retrieval shows the vectors are reliable |

## Post-MVP backlog

### Asked for by you (2026-11-04)

**1. Answer refinement for `ask`.** Answers are correct and cited, but they read like a list of evidence lines. Example: "which phase is finished in personal-os?" gave four sentences of the form "You finished phase N acceptance with the commit on …". Planned changes:

- **Shape answers to the question.** A list question gets a short list or table (phase, date, evidence). A "where did I leave off" question gets three parts: last state, what's unfinished, next step.
- **Lead with the answer:** one line first ("Phases 1–4 have acceptance notes; 3 and 4 await your review"), details after.
- **Human time formats:** "3 Nov, 19:01", or "yesterday evening", instead of "11-03 19:01".
- **Group duplicate evidence.** One phase or fact gets one line with several citations, not several near-identical sentences.
- **An optional verify pass:** the model checks its draft against the evidence and fixes misreadings like "2 errors" vs "2 failed runs". It costs a few seconds, so it's a setting.
- **A smarter router fallback.** When the rule-based router falls back to general search (as for the phase question), the model rewrites the question into a structured query (intent, project, time range). It's validated like any other model output.
- **Answer length setting:** brief or detailed.

**2. UI for answers and context.** Phase 6 builds the basic dashboard and overlay. After the MVP:

- answer cards where each citation expands to its evidence;
- a session timeline (swimlanes per project);
- search as you type;
- triaging the project inbox by drag and drop;
- per-project pages showing the current-state note;
- a polished phone layout.

**3. `personal-os llm warm` and `llm unload`.** *(Built early in phase 6, together with warming when the overlay opens; still open: unload when a game starts, and a "model warm" mark in the tray.)* Load the chat model into VRAM ahead of time when you know you'll be asking (`warm --for 30m`). This removes the cold-load wait (about 10 s now, more for bigger models later), and `unload` frees the 3.8 GB immediately. Background summaries don't use it; they load the model only while you're away. Planned hooks:

- warm automatically when the overlay hotkey opens (phase 6) or before a scheduled briefing (phase 7);
- unload automatically when a full-screen app or game starts;
- show "model warm" in the tray.

**3b. Friendlier `do` (2026-11-04).** Seen on your machine:

- "increase volume to 60" → *"Stopped: the planner failed: invalid structured output after a retry: media(…): times: Input should be less than or equal to 10"*
- "increase volume to 6" pressed volume-up once.

The safety held (nothing wrong ran), but the experience is poor. Planned:

- **Tools that match how people talk.** A `set_volume(percent)` tool that sets an absolute level through Windows' audio API, so "volume to 60", "volume 60%" and "turn it down a bit" all work. The same idea applies elsewhere: `set_brightness(percent)`, and absolute window positions instead of repeated key presses.
- **Plain-language failures.** When a plan can't be made valid, say what's possible ("I can set the volume to a level from 0 to 100, or press volume up/down"), never the validation text. The raw error goes to the log.
- **Ask when it's ambiguous.** "increase volume to 6": 6%, or 6 steps? A one-line question beats a guess for anything with a number.
- **Clamp and say so.** If a value is out of range (volume 150), use the limit (100) and tell you.

**3c. Voice polish (from phase 7).**

- Faster speech recognition: `base.en`, or the GPU when the chat model isn't loaded. It's about 3.6 s per phrase on the CPU today.
- An MCP *client*, to plug in more tools.
- Obsidian and other notes integrations (your Q6: later).
- Email and calendar *write* tools as tier 2 (send, create event).

### Suggested by me

4. **Streaming answers.** Show the answer while it's being written. It feels much faster at 30–45 tokens/s, and citations are still validated at the end.
5. **Follow-up questions.** "and before that?" or "what about portfolio-site?" reuse the last question's scope. This ties into conversation memory.
6. **Feedback on answers.** `ask --rate good|bad` (and thumbs in the UI) turns your real questions into a labelled set, so changes are measured on your data, not only the simulator's.
7. **Evidence reranking.** A second, finer ranking of the top ~40 evidence lines before answering: fewer irrelevant lines, shorter prompts, faster answers.
8. **Open threads across sessions.** Link a failing test or an open question from one session to where it was resolved later. Then "what's still open in auth-service?" has a precise answer.
9. **Model routing per task.** Small and fast for summaries, the largest model the GPU can hold for `--think` and planning, each set in config. Includes a check of free VRAM before loading (games, other GPU apps).
10. **Encrypted backup and restore** of the database, so moving to a new laptop doesn't lose your history.
11. **Linux and macOS observers** (the interfaces exist; the probes are stubs).
12. **VS Code extension** for editor context: open file, symbol, debug runs. The observer interface exists; the extension was deferred.
13. **Retention dashboard.** How much is stored per source and project, and what expires when, with one-click tightening.
