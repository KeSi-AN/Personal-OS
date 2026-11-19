# The live bench

The 1,100+ tests run on a scripted model, a fake desktop and a fake browser. They show each
part does what it was written to do, but every failure in the 16–19 Nov voice log had passed
them all. The bench measures what you actually get: your real requests, replayed through
Donna's real turn code with the real model.

```sh
uv run python -m personal_os.evals.bench                  # every case, about 5-10 minutes
uv run python -m personal_os.evals.bench --only music     # ids or tags containing "music"
uv run python -m personal_os.evals.bench --cold           # the prompt not read ahead (worst case)
uv run python -m personal_os.evals.bench --out docs/BENCH.md   # add the summary below
```

## What is real and what is pretend

| Real | Pretend (recorded, nothing happens) |
|---|---|
| The model (Ollama, your `[llm]` settings) | Windows: launching, closing windows, keys, volume |
| `Voice.respond`: rules, several-at-once, reminders, rest, router, agent, recall | The browser extension: tabs, media, pages, opening sites |
| The executor and its policy, every tool's own code | Windows' media sessions |
| A copy of your settings and database (taken at the start, deleted at the end) | Speech in and out, the popup, the island |
| Your Start-menu app list (`runtime/apps.json`) | Questions are answered from the case's `answers` |

The pretend machine has Brave open on GitHub, YouTube Music (playing "bad luck" by We Came As
Romans, unless the case says `playing = false`) and an Fmovies tab on Dune, plus VS Code and
Claude. Each case starts with no conversation and no rest, and, like Donna when idle with
`llm.keep_on_gpu`, with the agent's prompt already read (`--cold` turns that off).

## Cases

`personal_os/evals/cases.toml`: each says what you said, when you said it and what went wrong
then, and what a good answer does: tools that must run and succeed (with argument patterns),
whether nothing else may run, tools and effects that must not happen ("no Google search"),
words the reply must or mustn't contain, whether it should ask something, and a time limit.
Every case is also checked for the failures seen in the log: a crash, an empty reply, a
placeholder like "[list of overdue items]".

A case is **pass** (right and in time), **slow** (right, over its time) or **fail** (wrong).
Time is from the request to the reply being ready, not counting speech.

## Using it as a gate

Each run is saved under `<data dir>/bench/` (`latest.json` and one file per run), and the
next run prints what changed: `WORSE: music-pause pass -> fail` is a regression. Run it before
and after a change to how requests are understood; a change that makes a case worse doesn't
ship without a reason. When a real request goes wrong, add it as a case first.

The bench shares Ollama with a running Donna, so her prompt has to be read again afterwards
(about 30 s on the GTX 1650, done by her next priming).

## Results

### 2026-11-20 02:46 (first baseline): 30/34 pass, 0 slow, 4 wrong

Model qwen3:4b, prompt read ahead. Median 0.0 s, 90th percentile 11.1 s.

| group | right and in time | median |
|---|---|---|
| app | 4/4 | 0.1 s |
| browser | 0/1 | 31.7 s |
| chat | 0/1 | 12.5 s |
| greet | 3/3 | 0.0 s |
| multi | 4/4 | 2.8 s |
| music | 8/9 | 4.6 s |
| page | 1/1 | 0.0 s |
| projects | 1/2 | 7.9 s |
| recall | 1/1 | 10.5 s |
| remind | 5/5 | 0.0 s |
| rest | 2/2 | 0.0 s |
| volume | 1/1 | 0.0 s |

Not passing:

- `chat-thanks` (fail, 12.5 s): nothing else (project_items(project='Personal-OS', show='todos') -> succeeded: 5 item(s) in Personal-OS;)
- `music-open-then-play` (fail, 11.1 s): does play_media(query~blood ?sport) (not called); reply ~blood ?sport (Opened Youtube. I couldn't switch tab the first tab: No open tab has "the first tab" in it)
- `projects-left-off-shop` (fail, 15.8 s): reply ~yesterday|19 (You committed Phase 5 (part 1) with 24 files and significant changes to the shop-app proje); reply !~13 (You committed Phase 5 (part 1) with 24 files and significant changes to the shop-app proje)
- `browser-what-am-i-watching` (fail, 31.7 s): reply ~dune (I see you're idle with Brave open to Fmovies. Let me check what's playing there for you.); within 20 s (31.7 s)

What this baseline says, read with the 16-19 Nov log:

- **Rules carry most of it, and they're right and instant**: greetings, music controls,
  reminders (including "in 5" and a missing message asked for), rests, several requests at
  once, opening and closing apps, volume, the island's pages. In the log these took 60-82 s or
  went wrong; the fixes of 19-20 Nov (ADR-087, commit 1ea33d9) are what changed.
- **One model step with the prompt already read is 4-7 s** ("play blood sport by sleep token",
  "play zulfe and then take 5"), against 26-86 s in the log, where the prompt was read afresh.
- **The four failures are the model choosing badly and the project model**, not speed:
  "thanks, that's all" ran three project lookups and listed to-dos; "open youtube music and
  play blood sport there" opened YouTube and tried to switch tabs instead of `play_media`;
  "check inside fmovies" said "let me check" and stopped without reading the tab (31.7 s);
  "where did I leave off on shop-app" answered 13 Nov and missed the 19 Nov commits.
- **Not covered here**: speech recognition (cases are typed, so "braille" for Brave can't
  happen), a prompt pushed out by recall or another app using Ollama, and the real browser's
  timing. Spoken cases from recordings are the next step for the bench.

### 2026-11-20 03:09 (after the fixes, ADR-089): 34/34 pass, 0 slow, 0 wrong

Model qwen3:4b, prompt read ahead. Median 0.0 s, 90th percentile 6.7 s.

| group | right and in time | median |
|---|---|---|
| app | 4/4 | 0.1 s |
| browser | 1/1 | 10.3 s |
| chat | 1/1 | 0.0 s |
| greet | 3/3 | 0.0 s |
| multi | 4/4 | 0.0 s |
| music | 9/9 | 0.0 s |
| page | 1/1 | 0.0 s |
| projects | 2/2 | 6.2 s |
| recall | 1/1 | 12.5 s |
| remind | 5/5 | 0.0 s |
| rest | 2/2 | 0.0 s |
| volume | 1/1 | 0.0 s |

Each first-run failure was fixed where it came from, and two new problems seen on the way
(a model choice that changed with the prompt, and a filter that ate a reminder's reply) are
now cases too. A song by name, opening a service and playing on it, and "say hi once you're
back" are now rules, so 30 of 34 cases never reach the model; the four that do (what's
playing, recall, where you left off, what's in a tab) take 5-13 s. The reminder cases now
also check the reply's words.
