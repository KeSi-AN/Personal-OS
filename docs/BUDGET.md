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

The phase 1 acceptance run (1 hour of normal work) is still to do; append its result here.
