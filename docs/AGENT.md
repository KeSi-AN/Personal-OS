# Donna as an agent: design

**Goal (your words):** a fully autonomous assistant, completely local, that keeps track of your day and work, does things when you ask, and is a proper voice companion.

**Constraints:**
- **Hardware:** a GTX 1650 with 4 GB, so the brain is `qwen3:4b` through Ollama.
- **Privacy:** nothing leaves the laptop. The existing safety layer is kept as it is: risk tiers, confirmations, taint, the audit log and the kill switch.

## Why Donna felt task-only

1. **Fixed lanes.** A first-word router sent each request down one of three paths. *ask* searched your activity, *do* picked tools once, and *chat* gave canned lines. Nothing simply talked to you or thought with you.
2. **One handler per question.** Usage, visits and reminders each had their own code, so every new question meant new code.
3. **No picture of "now".** History was recorded, but what's open right now (windows, tabs) wasn't, so "what's open on Brave?" had nothing to read.
4. **A small model doing open-ended work.** A 4B model fills a fixed form well and plans long chains poorly. The design has to play to that strength.

## The shape

```
        you (voice, overlay, CLI)
                 |
          fast paths (rules)        open X, volume, reminders, stop, thanks: instant
                 |  anything else
          AGENT LOOP (qwen3:4b, JSON, up to 4 steps)
           |  each step: pick tool calls, or answer
           v
    general capabilities ------------------------------------------
    now        what's open and happening right now (windows, tabs, idle, project)
    recall     your history, answered with evidence (the old `ask`, now a tool)
    search     raw search over what you've done
    reminders  list / set / change / cancel
    memory     facts you confirmed
    desktop    open, windows, media, volume, brightness, screenshot, lock
    files      list, read, create, edit (confirmed)
    git        commit, stash, switch (confirmed)
    commands   your named commands (confirmed)
    calendar / mail (read-only, when Google is connected)
    ---------------------------------------------------------------
           |
     policy + audit (unchanged): tier 0 runs, tier 1 runs when you asked for it directly,
     tier 2 always asks, tier 3 blocked; anything derived from captured text or the earlier
     conversation asks first; kill switch wins
```

### The agent loop (phase 9a)

- **One loop for everything that isn't a fast path.** Each step, the model returns `{calls, reply}`:
  - calls run through the executor;
  - their results go back to the model as data, never as instructions;
  - the loop ends when the model answers without calls, or after 4 steps.
- **General conversation is allowed.** Questions about the world, help with writing and small talk are answered from the model's own knowledge. Anything about *your* computer, day or work must come from a tool.
- **A recall-only answer is passed straight through.** When the only call was `recall` (the existing cited-answer pipeline), its answer is the reply. That avoids a second model call and keeps the citations.
- **Questions never change anything.** The router sends questions to the agent as questions, and the agent's prompt says so. The planner's provenance check (ADR-074) also applies.

### "Now" (phase 9a)

`context/now.py` builds a snapshot on demand:

- **Windows:** every visible window, with its app and title, and which one is in front (read directly from Windows, so it's always current).
- **Browser tabs:** the extension sends the list of open tabs whenever it changes, and once a minute while the browser runs. The observer keeps only the latest list per browser, in `runtime/now/`, never in the database.
  - Incognito tabs are never sent.
  - Blocked domains and non-web pages are dropped.
  - URLs are redacted like everything else.
- **Your state:** idle or active, paused, the kill switch, the current session's project, and the next reminders.

It's a tool like any other: its output is captured content, so it taints what follows.

### Later phases (each its own review)

| Phase | What | Why |
|---|---|---|
| 9b **Proactive Donna** | A quiet loop every 10–15 minutes while you're active. It looks at `now` and the day's activity, and may *offer* help (a popup card plus one spoken line): a break after a long stretch, a meeting soon, a failing build left behind, a reminder you might want. It never acts without you. It's rate-limited, silent during full-screen apps and quiet hours, and has an off switch. | "Keeps track of my day" without nagging |
| 9c **Skills as data** | "When I say *start work*, open VS Code on Personal-OS and tell me what's open" is saved as a short recipe (Markdown) that the agent follows with its tools. You add them by saying them. | New abilities without new code |
| 9d **MCP client** | Local MCP servers (files, git, Spotify, Obsidian…) become agent tools through the same policy | The ecosystem instead of hand-written tools |
| 9e **Seeing any app** | Windows UI Automation reads the visible text of the app in front ("what does this error say?"), read-only, as captured content | "What's on my screen" for any app, not only browsers |
| 9f **Voice that feels live** | Speak the first sentence while the rest is still being written; a "thinking" sound if a step takes more than a second; and an end-of-turn detector instead of a fixed pause | A proper voice companion |
| 9g **A bigger brain when it fits** | Model routing: `qwen3:4b` for steps, a larger model with thinking for hard multi-step requests when the GPU is free (games closed), and a check of free VRAM | Better reasoning without leaving the laptop |
| 9h **Browser and website control** (built; see below) | Through the extension: open pages and searches, play a song or video by name, switch, close or mute tabs, control what's playing, read the page, and click or type on it (always confirmed) | Most things you do happen in the browser; one general set of tools instead of one tool per site |

## Risks and limits

- **The 4B model will sometimes pick the wrong tool or misread a result.** Mitigations:
  - few, clearly described tools;
  - argument validation with one retry;
  - every action shown back in its reply;
  - undo for anything reversible.
- **Prompt injection.** Text from tools (pages, files, window titles) is data. After it is read, every action in that request needs your yes (ADR-050), and tier 3 stays blocked.
- **Latency.** Each step is a model call (about 1–3 s warm). The fast paths stay for the common commands, `recall` answers pass straight through, and phase 9f makes waiting audible.
- **Autonomy is bounded on purpose.** Donna proposes and you approve, except for low-risk things you asked for directly. "Fully autonomous" here means she can chain steps, look things up and come to you first. It doesn't mean she acts on your accounts unasked.

## Browser control (phase 9h)

```
Donna --> browser tool --> runtime/browser/cmd/<id>.json
                                 |  the native host (inside the browser) claims it
                                 v
                 extension: open / read / click / media ...  --> result: runtime/browser/res/<id>.json
```

There's no port, and web pages can't reach this channel. Each connected browser leaves a marker in `runtime/browser/hosts/`, and `doctor` shows it on its `control` line. A command nobody picks up within 30 s is dropped, never run late.

| Tool | Does | Risk |
|---|---|---|
| `browser_open` | open a URL, or search a site (Google, YouTube, YouTube Music, GitHub, Wikipedia, Maps, Stack Overflow, Amazon) | tier 1 |
| `play_media` | play a song or video by name: search YouTube Music or YouTube, then open the first result, which plays | tier 1 |
| `browser_tab` | switch, close, back, forward, reload, mute or unmute, by words from a tab's title | tier 1 |
| `browser_media` | play, pause, next, previous or seek whatever's playing (it can't choose a song) | tier 1 |
| `whats_playing` | the song or video playing now, from the page's media info | tier 0, captured |
| `browser_read` | the page's text and its numbered buttons, links and fields | tier 0, captured |
| `browser_act` | click, type, press a key or scroll | tier 2: always asks |

**Sites are data.** `personal_os/browser/recipes.py` holds a search URL and, where it applies, a "first result" selector for each site. Adding a site means adding a line there, not writing a tool.

**Limits, enforced by the extension:**
- http(s) pages only;
- never incognito;
- never reads or types into password fields, and won't act at all on a page that shows one.

Reading a page counts as captured content, so a later action needs your yes, and page text can't be sent out by a later network call. Browser pages (`brave://`, the Web Store, the PDF viewer) are out of reach for any extension.

The host learns which browser it's in from its process tree. Brave also reads Chrome's registration, so the launcher's argument alone said "chrome".

## Next (requested 2026-11-07)

| Phase | What | Why |
|---|---|---|
| 9i **Show, then say** (built) | The popup shows the answer **before** Donna speaks: voice waits (at most about 0.5 s) for the popup to confirm it has drawn this turn's reply. For actions, she says what she's about to do ("Playing Circles by Post Malone") and **then** does it, and speaks again only if it fails. The agent gets a short spoken "on it" line from the tool's own description while a step runs. | You saw replies after hearing them, and heard "playing" after the music had already started |
| 9j **A popup that shows the whole answer** (built) | Long or structured answers (lists, tabs, links, evidence) expand the popup into a scrollable card with clickable links. Donna speaks a short version and says "it's on screen" instead of "the rest is on the dashboard". Pin keeps it open. | Answers were cut short with nowhere to read the rest |
| 9k **Projects you curate** (built) | Sources (links, browser tabs, whole tab groups, files), notes, to-dos and "resume" (reopen the project's tabs as a group), by voice ("add these tabs to Personal-OS"), through the extension's right-click **Add to project**, or on the dashboard. Attached sources teach the tracker which pages belong to which project; the model's guesses become suggestions. | The local model can't track projects precisely; you can, in one sentence |
| 9l **Dashboard v2** (built) | A new dashboard: Today (time spent, next up, where you left off, timeline), Projects (overview, sources, notes, to-dos, timeline, resume), Donna (the full conversation with evidence, actions and undo), Notes, Settings. The old dashboard is removed once the new one replaces it. Design: [DASHBOARD_V2.md](DASHBOARD_V2.md) | The current dashboard is about the system, not your day |
| 9m **Donna knows herself** (built) | An `about_me` tool: is the wake word on, which voice, the hotkeys, what's connected (doctor's facts). A `conversation` tool: "what did we talk about?" reads the conversation history, not your activity. | "Is the wake word active?" and "show me our recent chats" were answered by searching your activity |

### Memory check (2026-11-07), and what it showed

Shared memory works: follow-ups see both sides of the conversation, voice and typed. In the log, "the first three tabs" was resolved from the turn before. Two weaknesses showed up, to fix in 9i:

- **She can repeat her own earlier mistake.** After the extension refused a command it didn't know yet ("unknown command tabs.group"), her reply copied her earlier wrong sentence word for word: "I can't group tabs… a feature of the browser itself". The prompt should say that earlier replies can be wrong, and that only this turn's tool results count. A false "I can't" should be corrected the way false "I did" claims already are.
- **Copied context is guarded but noisy.** A call built from earlier words ("which: first three") needed a yes, as designed. It was declined, because the request had used "YouTube", not positions.

## Phase 10: refinements (2026-11-11)

**Fewer "Sorry, I couldn't work that out."** The logs showed the failures were tool calls that wouldn't validate, not the model giving up:
- it wrote `project?` as an argument name, copied from the tool list;
- it gave reminder times longer than 12 characters;
- it used actions a tool doesn't have.

The fixes:
- argument names are cleaned before checking;
- longer times ("tomorrow 9am", "this evening 7") parse;
- a step that still won't validate gets an answer from what's known instead of an apology;
- a question about the past is no longer taken for a false claim;
- an action waiting for approval says so.

**Where you left off / where to begin** now includes the project's open to-dos, latest notes and saved sources, cited, and leads with them.

**How time was spent** (`context/activity.py`): every minute goes into one of these categories.

| Category | Examples |
|---|---|
| Coding and building | VS Code, Cursor, terminals, Claude, GitHub, local dev servers |
| Research and learning | docs, Stack Overflow, AI chats, job portals, YouTube tutorials and talks |
| Chill | YouTube music, songs, episodes, Shorts, streaming and anime sites, social media |
| Gaming | Steam, Riot and Epic games, anything in a games folder |
| Messages and calls | mail, chat apps, meetings |
| Other, idle | everything else; up to 30 minutes without input |

YouTube is judged by the video, not the site, so learning there is research. Titles the rules can't place are asked of the local model once, and the verdict is cached.

**R&D projects** (`projects/research.py`): learning that no project claims, with 10 or more minutes or 2 or more pages on one topic, becomes "R&D: <topic>". The videos and pages are attached so you can come back to them, or rename it into a real project. An existing project on the topic gets the links instead.

**Modes** (`core/modes.py`):

| Mode | Effects |
|---|---|
| Work | a nudge when chill takes 15 of the last 20 minutes (at most every 30) |
| Gaming | local model unloaded, background jobs paused, no wake word, popups, follow-ups or spoken announcements; push-to-talk still works. Starts by itself while a game is in front and ends 3 minutes after. |
| Chill | wake word on, no nudges |

**The brief** (`/brief`):
- a plain-language summary of the last 24 hours;
- a category donut, an hour-by-hour chart, projects, apps and sites;
- research and the R&D projects kept, what stood out, modes, and the next to-dos.

It opens in the browser 45 seconds after capture starts, at most every 6 hours.
