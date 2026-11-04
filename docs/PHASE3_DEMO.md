# Phase 3 demo: projects, inbox and sessions

Phase 3 gives every captured event a project and a confidence. Every git repo the daemon has seen becomes a project on its own. The daemon assigns new events every 30 seconds:

- **Path signals:** repo, file path, terminal folder.
- **Window titles.**
- **GitHub pages** of your repos.
- **Rules learned from your corrections.**
- **Words a page shares with a project.**
- **Timing:** what you were doing around it.

Anything it isn't sure about goes to an inbox, grouped so one command fixes many items. Every 5 minutes it also rebuilds your **work sessions**: blocks of time on one project, with apps, files, pages, commands and commits.

There's a built-in project called `none`, for anything that isn't project work: streaming, personal mail, chats.

## 1. Tests and the labelled evaluation

```powershell
uv run pytest -q tests/test_projects.py
uv run personal-os sim eval-projects tests/fixtures/scenarios/labeled_two_days.toml
uv run personal-os sim eval-projects tests/fixtures/scenarios/labeled_holdout.toml
```

`eval-projects` replays two labelled simulated days in a temporary database:

- **Day 1** is a cold start: three projects plus personal browsing.
- **Corrections** are then made the way you'd make them, through the inbox.
- **Day 2** is scored, by events and by time.

The command prints every mistake and every correction.

## 2. Projects

```powershell
uv run personal-os projects                     # all projects, events and time in the last 7 days
uv run personal-os projects add "Thesis" --root "C:\Users\you\Documents\Thesis" --keyword thesis
uv run personal-os projects rename personal-os "Personal OS"
uv run personal-os projects keyword personal-os edith resolver
uv run personal-os projects archive old-repo    # --undo to restore
```

Adding a project or a keyword re-assigns the last 30 days straight away.

## 3. Inbox and corrections

```powershell
uv run personal-os inbox
```

Each line is a group: a site, an app (with a folder or title where that helps), and how many items and how much time it covers. If the resolver has a weak guess, that's shown too. Then:

```powershell
uv run personal-os assign 3 personal-os         # inbox line 3 belongs to personal-os
uv run personal-os assign 5 none                # line 5 isn't project work
uv run personal-os assign domain:docs.python.org personal-os
uv run personal-os assign <event-id> personal-os            # just this one event
uv run personal-os assign <event-id> personal-os --learn    # ... and learn a rule from it
```

Assigning a group teaches a rule: the same site or app goes to that project from now on. Assigning a single event only fixes that event, unless you add `--learn`.

Run `inbox` again afterwards. Everything the new rule covers has moved out of it.

## 4. Sessions

```powershell
uv run personal-os sessions                     # today
uv run personal-os sessions --day 2026-11-03
```

Each session shows:

- Its time range and project.
- Active time.
- A confidence: how much of the session's time the project actually accounts for.
- Apps, files, pages, commands (and how many failed), and commits.

A short detour doesn't split a session. A break of 15 minutes or more does.

## 5. Check by hand

```powershell
uv run personal-os resolve            # assign pending events now instead of waiting 30 s
uv run personal-os resolve --all      # re-decide everything you haven't corrected
uv run personal-os events --last 20   # each line shows its project
```

## Acceptance (brief §10)

> ≥90% correct project assignment on a labeled test day.
