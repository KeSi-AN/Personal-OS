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

## Risks and limits

- **The 4B model will sometimes pick the wrong tool or misread a result.** Mitigations:
  - few, clearly described tools;
  - argument validation with one retry;
  - every action shown back in its reply;
  - undo for anything reversible.
- **Prompt injection.** Text from tools (pages, files, window titles) is data. After it is read, every action in that request needs your yes (ADR-050), and tier 3 stays blocked.
- **Latency.** Each step is a model call (about 1–3 s warm). The fast paths stay for the common commands, `recall` answers pass straight through, and phase 9f makes waiting audible.
- **Autonomy is bounded on purpose.** Donna proposes and you approve, except for low-risk things you asked for directly. "Fully autonomous" here means she can chain steps, look things up and come to you first. It doesn't mean she acts on your accounts unasked.
