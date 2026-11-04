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
| 5 | Action layer: tool registry, tiers, confirmation, audit log, kill switch, injection tests | Built, awaiting review |
| 6 | Tray, hotkey overlay, web dashboard (reachable from your phone), privacy centre | Planned |
| 7 | Voice, briefings, MCP/plugin API, Google Calendar and Gmail | Planned |

## Gaps: brief requirements still open (close before the MVP is done)

| Requirement (brief section) | Where it lands |
|---|---|
| Export of everything stored, as JSON and Markdown (§5) | Phase 6, privacy centre (CLI command first) |
| Per-project delete (§5) | Phase 6, privacy centre (CLI command first) |
| Conversation memory: short-term chat context, plus long-term facts you confirm ("remember that…"), stored separately and editable (§8) | Phase 6, with the overlay chat |
| Per-project timeline: daily and weekly summaries, plus a living "current state" note (§5). Deferred in phase 4; weekly questions are answered from session summaries at question time | Phase 6, project pages |
| First-run wizard that explains each source before enabling it (§9) | Phase 6 |
| Tier 1 tools that need notifications: brightness, do-not-disturb, timers and reminders (§7) | Phase 6, with the tray |
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

**3. `personal-os llm warm` and `llm unload`.** Load the chat model into VRAM ahead of time when you know you'll be asking (`warm --for 30m`). This removes the cold-load wait (about 10 s now, more for bigger models later), and `unload` frees the 3.8 GB immediately. Background summaries don't use it; they load the model only while you're away. Planned hooks:

- warm automatically when the overlay hotkey opens (phase 6) or before a scheduled briefing (phase 7);
- unload automatically when a full-screen app or game starts;
- show "model warm" in the tray.

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
