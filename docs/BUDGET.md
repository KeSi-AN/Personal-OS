# Resource budget

Target (brief §4.7): idle CPU under about **2%** of the machine and memory under **300 MB** for the whole daemon, excluding the LLM.

**How it's measured:** [scripts/measure_budget.py](../scripts/measure_budget.py) finds the daemon's process through its health file and samples it with `psutil` every few seconds. It reports CPU as a percentage of the whole machine (all 8 logical CPUs), the way Task Manager does, along with resident memory. Each run appends a section below.

```powershell
uv run python scripts/measure_budget.py --minutes 60 --out docs/BUDGET.md
```

## Results

### 2026-11-02 01:47 — 100 samples over 5.0 min (development check, not the acceptance run)

- Setup: all observers on (window, session, fs on `Documents`, git across 26 repos, terminal), with normal use and active editing in this repo. A full-screen game was also running at the time.
- Machine: 8 logical CPUs, 15.8 GB RAM, AC power
- CPU (% of machine): median **0.06%**, p95 0.45%, max 0.53% (budget < 2.0% idle)
- Memory (RSS): median 50 MB, max **55 MB** (budget < 300 MB)
- Result: **within budget**

Startup costs a short burst, about 1.5% for the first few seconds, while git takes a baseline of every repo.

The PowerShell hook adds about **8–11 ms** per prompt on this machine (measured by `tests/test_hooks.py -m live` over 20 prompts in Windows PowerShell 5.1). That's below what you'd notice, but above the 3 ms first estimated in the design: PowerShell 5.1 is slow at `Get-History` and string work.

### 2026-11-03 18:30 — phase 1 acceptance run, 26.7 min of normal use

- Setup: real data dir, all observers on (window, session, fs on `Documents`, git across 26 repos, terminal hook in PowerShell). Usage covered a video call, VS Code, File Explorer, the browser, terminal commands, a commit and a `git pull`.
- Machine: 8 logical CPUs, 15.8 GB RAM, AC power
- CPU (% of machine): **0.065% average** over the whole run (8.3 CPU-seconds in 26.7 min, from the OS's own process CPU time)
- Memory: 46 MB at the end, **53 MB peak** working set
- Bus: 235 events received, 235 stored, 0 dropped, high-water 15; no filter errors, no observer restarts
- Result: **within budget**

You chose to stop at about 25 minutes instead of 60, so this uses the daemon's lifetime CPU time rather than the sampling script. That's the same measure, averaged over the whole run instead of 5-second samples.

## Voice popup (phase 8)

The popup is a separate process that exists only while it's on screen, so it isn't part of the daemon's budget. Measured with:

```powershell
uv run python scripts/measure_budget.py --popup 60
```

This opens the demo pinned and looping (so the listening, thinking and speaking animations keep running, which is the worst case), samples the Python host and its WebView2 processes every second, closes it, and checks that every one of those processes has exited.

### 2026-11-06 01:11, 59 samples over 60 s

- Open (demo, pinned, animating): 7 processes (Python host, and WebView2's browser, GPU, renderer, two utility processes and crash handler)
- Renderer up 1.5 s after the spawn
- CPU (% of machine): median **1.71%**, max 9.15% (the max is a state change; the Python host polling the runtime files is about 0.15% of that)
- Memory: private (USS) median **145 MB**, max 167 MB. The sum of RSS was 491 MB, but that counts memory shared between WebView2's processes several times.
- Closed: **every popup process exited**, so the cost while closed is zero.

A first run, with the speaker bars animating `height`, used 2.65% median CPU. Animating `transform` instead (no layout every frame) brought it to 1.71%. A real turn keeps the popup open only for the turn plus 5 seconds.

## Every always-on process (audit phase 4)

The daemon was last measured in phase 1. Since then voice (the mic, echo cancelling, the
wake word) and the island run all day too, so `measure_budget.py --all MINUTES` samples each
of them on its own (the island with its WebView2 processes), in normal use with the model
kept on the GPU (Ollama's own memory isn't counted here).

### 2026-11-21 01:44 — every process, 10 min

| process | CPU median | CPU p95 | memory (private) median | max |
|---|---|---|---|---|
| launcher | 0.00% | 0.00% | 22 MB | 22 MB |
| daemon | 0.11% | 2.05% | 77 MB | 79 MB |
| voice | 2.40% | 3.44% | 376 MB | 447 MB |
| island | 0.28% | 2.94% | 154 MB | 197 MB |

- Daemon budget (< 2.0% CPU idle, < 300 MB): **within budget**

- The daemon has grown from 53 to about 78 MB with phases 3-11 (projects, sessions, model
  jobs, modes, dashboard) and stays inside its budget.
- Voice is the steady cost: about 2.4% of the machine (a fifth of one core) with the wake word
  on. Most of that is Vosk decoding every word it hears: offline, free-form decoding took 14%
  of a core against 3.4% with a phrase list (`voice.wake_grammar`, off by default: in 8
  synthetic sentences it woke once on "I don't know", where free-form never did; see
  `voice/wake.py`). Its 376 MB are mostly the two Whisper models and the echo canceller.
- A first run of `--all` counted each process with its children, which here means launcher,
  daemon, voice and island all over again (900 MB "each"); it now samples each one alone.
