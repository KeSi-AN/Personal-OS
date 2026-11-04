# Phase 7 demo: voice, briefings, Google, MCP and plugins

Phase 7 adds four things:

- **Voice:** push-to-talk, an optional "hey Donna" wake word, and spoken replies.
- **Briefings:** a morning briefing and an end-of-day summary.
- **Google:** Calendar and Gmail, read-only.
- **MCP and plugins:** a read-only MCP server for other assistants, and a plugin API for your own tools.

## 1. Tests

```powershell
uv run pytest -q tests/test_phase7.py      # routing, voice handling, briefings, Google (faked), plugins, MCP
```

## 2. Hands-free, without a microphone first

```powershell
uv run personal-os voice demo
```

Two phrases ("Hey Donna, brief me" and "Hey Donna, remind me in two minutes to stretch") are spoken by the Windows voice into audio. Then each one is:

1. transcribed by the local speech model;
2. handled;
3. answered aloud.

The reminder is real: it pops up from the tray two minutes later.

## 3. With your voice

```powershell
uv run personal-os voice on
uv run personal-os voice status
```

Press **Ctrl+Alt+Shift+V**, wait for the beep, and say any of these:

- "brief me" or "end of day";
- "where did I leave off on personal-os?";
- "remind me in ten minutes to stand up";
- "take a screenshot";
- "remember that standup moved to 10:30" (it asks "yes or no?" first);
- "stop" (stops it speaking).

docs/VOICE.md has the full list, training "hey Donna", and setting up Speechify or Piper.

## 4. Briefings

```powershell
uv run personal-os briefing             # morning
uv run personal-os briefing --evening   # end of day
uv run personal-os briefing --speak
uv run personal-os briefing --enable    # one of each per day, automatically
```

The dashboard's Today page also has the latest briefing and a **Brief me now** button.

## 5. Google Calendar and Gmail

Follow docs/GOOGLE.md (about 10 minutes, once), then:

```powershell
uv run personal-os google connect "C:\Users\you\Downloads\client_secret_....json"
uv run personal-os google status
uv run personal-os do "what's on my calendar today?"
```

Briefings then include today's events and your unread mail: the senders and subjects only, never message bodies.

## 6. MCP: your context in Claude Desktop

```powershell
uv run personal-os mcp --config      # prints the entry for Claude Desktop's config
```

Add it under Settings → Developer → Edit Config, and restart Claude Desktop. It can then call `ask`, `search`, `sessions`, `project_state` and `list_projects`. These tools are read-only, with no actions.

## 7. Plugins

```powershell
uv run personal-os plugins example                       # writes plugins\word_count.py
uv run personal-os config set actions.plugins_enabled true
uv run personal-os plugins list
uv run personal-os do "count the words in: the quick brown fox"
```

## Acceptance (brief §10)

> Hands-free demo of a briefing and a safe task.
