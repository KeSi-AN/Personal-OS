# Dashboard v2: proposal

**Status:** proposal, not built. It replaces the phase 6 dashboard, which you've judged not good enough. The safety model stays as it is:
- it runs on this laptop only (127.0.0.1), with a one-time login link;
- every change goes through the same policy as voice;
- no page loads anything from the internet (ADR-055).

## What's wrong with the current one

- **It's a set of pages about the system** (sources, inbox, sessions, privacy), not about **you**: your day, your projects, what's next.
- **Answers are cut short.** Voice says "the rest is on the dashboard", but the dashboard has no conversation view to show it in.
- **Project tracking is guesswork.** Projects are inferred from activity by rules and a small model. You can't say "these tabs belong to this project", and there's nowhere to keep notes, to-dos or links to come back to.
- **It looks like an admin panel:** dense tables, no hierarchy, nothing you'd keep open.

## The new shape

A single window with a left rail and five places. It's keyboard-first (`Ctrl+K` jumps anywhere), works in light or dark, and is usable on the phone (read-only there, as now).

```
┌──────────┬─────────────────────────────────────────────────────────────┐
│ Today    │  TODAY  Thu 7 Nov                       ▶ Circles · Post Malone │
│ Projects │  ┌ Time ──────────────┐ ┌ Next ───────────────┐ ┌ Donna ──────┐ │
│ Donna    │  │ 5h 12m focused     │ │ 15:00 Standup       │ │ ask anything│ │
│ Notes    │  │ ▇▇▇▅▂ Personal-OS  │ │ ⏰ 16:30 stretch     │ │             │ │
│ Settings │  │ ▃▂ Portfolio       │ │ ☐ fix popup timing  │ │ last answer │ │
│          │  └────────────────────┘ └─────────────────────┘ └─────────────┘ │
│          │  Where you left off: Personal-OS, browser-control, tests…     │
│          │  Timeline ─────────────────────────────────────────────────── │
└──────────┴─────────────────────────────────────────────────────────────┘
```

### 1. Today (the home page)
- **Time spent today:** total focused time, and a bar per project. Each bar opens the project.
- **Next up:** calendar events, reminders, and the day's to-dos, all in one list.
- **Where you left off:** the last state of the project you were in, with "resume", which reopens its sources.
- **Now playing / open now:** from the live `now` state.
- **Timeline:** today's sessions as blocks per project; click one to see what happened.
- **Donna card:** type or talk, and see the latest answer in full.

### 2. Projects
One page per project. It's the answer to "tracking isn't precise enough": you curate it, and the model only suggests.

| Section | What it holds | How things get in |
|---|---|---|
| **Overview** | time this week and today, last worked on, the current-state note | automatic (sessions, rollups) |
| **Sources** | links, browser tabs and **whole tab groups**, files and folders, each with a title and a note | "Donna, add these tabs to Personal-OS"; the extension's right-click **Add to project**; drag a link in; a Brave tab group named like the project stays in sync |
| **Notes** | quick Markdown notes, newest first | typed here, or "note for Personal-OS: …" by voice |
| **To-dos** | checkbox items, optional due date, which become reminders | typed, by voice, or "make that a to-do" in chat |
| **Timeline** | sessions, commits and pages, by day | automatic |
| **Resume** | reopens the project's sources as a Brave tab group, opens the repo in VS Code, and shows the last note | one button, or "Donna, resume Personal-OS" |

Sources you add **teach the tracker**. A page from a domain or tab group you've attached is credited to that project from then on. This replaces fuzzy guessing with your own choices. The model's guesses still appear as **suggestions**, to accept or dismiss.

### 3. Donna
- **The full conversation**, voice and typed, with every answer complete: lists, tables, links and the evidence behind each claim (click a citation to see it).
- **Actions she took**, inline (Opened…, Grouped 5 tabs…), with **Undo** where it applies.
- **Pending confirmations,** to approve or decline.

### 4. Notes
All notes and to-dos across projects, searchable, plus an **Inbox** for things not yet filed.

### 5. Settings
- **Privacy:** sources, blocked sites, pause, forget, export.
- **Voice:** wake word on/off, voice, follow-up window.
- **Phone access,** and the browser connection.

Everything from the old Privacy and Sources pages moves here, so nothing is lost.

## How it's built

- **The same server, inside the daemon** (FastAPI). It's served only to this laptop, with the one-time login link, and a strict content policy.
- **No build step, still.** Plain HTML and CSS with a proper design system:
  - one set of colour, spacing and type tokens for light and dark;
  - cards and a responsive grid;
  - one small vendored script, htmx (about 14 KB, stored in the repo, never loaded from the internet), for partial page updates without writing a front-end app.
- **New tables:** `project_items` (kind: link, tab, tab group, file, note or to-do; project; title; url or path; body; done; due; created; source). One migration. Export, `forget` and per-project delete include it.
- **New agent tools,** so everything works by voice too:
  - `project_add_source` (the current tab, a tab group or a link), `project_note`, `todo_add` and `todo_done`;
  - `project_resume`, which reopens the sources as a tab group.
- **Extension:**
  - a right-click **Add to project** menu (pages and tab groups);
  - tab groups named after a project are kept in sync with it.

## The order I'd build it in

1. Data and tools: `project_items`, sources, notes, to-dos and resume, with the voice and agent tools.
2. Today and Projects pages, with the old dashboard kept at `/old` until you're happy.
3. The Donna page (full answers, actions, undo, confirmations).
4. Notes, Settings, the extension's **Add to project**, and tab-group sync.
5. Remove the old dashboard.
