"""Measure the running daemon's CPU and memory against the budget (DESIGN.md §4.12).

    uv run python scripts/measure_budget.py --minutes 60 [--out docs/BUDGET.md]

Samples the daemon process (found via the health file in the data dir) every 5 s.
CPU is reported as a percentage of the whole machine (all cores), like Task Manager.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

from personal_os.config.loader import load_config
from personal_os.core import runtime

CPU_BUDGET = 2.0
RSS_BUDGET_MB = 300


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--out", type=Path, help="Append a Markdown result section to this file.")
    args = ap.parse_args()

    cfg = load_config()
    info = runtime.daemon_info(cfg.runtime_dir)
    if not info.running or info.pid is None:
        print("Daemon not running. Start it with `personal-os daemon start`.", file=sys.stderr)
        return 1
    proc = psutil.Process(info.pid)
    ncpu = psutil.cpu_count() or 1
    proc.cpu_percent(None)
    cpu: list[float] = []
    rss: list[float] = []
    end = time.monotonic() + args.minutes * 60
    started = datetime.now().astimezone()
    print(f"Sampling pid {info.pid} every {args.interval:.0f}s for {args.minutes:g} min...")
    while time.monotonic() < end:
        time.sleep(args.interval)
        try:
            cpu.append(proc.cpu_percent(None) / ncpu)
            rss.append(proc.memory_info().rss / 1e6)
        except psutil.NoSuchProcess:
            print("Daemon exited during the measurement.", file=sys.stderr)
            break
    if not cpu:
        return 1
    battery = psutil.sensors_battery()
    power = "unknown" if battery is None else ("AC power" if battery.power_plugged else "battery")
    p95 = sorted(cpu)[max(0, int(len(cpu) * 0.95) - 1)]
    ok = statistics.median(cpu) < CPU_BUDGET and max(rss) < RSS_BUDGET_MB
    minutes = len(cpu) * args.interval / 60
    ram_gb = psutil.virtual_memory().total / 2**30
    med_cpu, max_cpu = statistics.median(cpu), max(cpu)
    med_rss, max_rss = statistics.median(rss), max(rss)
    report = "\n".join(
        [
            f"### {started:%Y-%m-%d %H:%M} — {len(cpu)} samples over {minutes:.1f} min",
            "",
            f"- Machine: {ncpu} logical CPUs, {ram_gb:.1f} GB RAM, {power}",
            f"- CPU (% of machine): median **{med_cpu:.2f}%**, p95 {p95:.2f}%, "
            f"max {max_cpu:.2f}% (budget < {CPU_BUDGET}% idle)",
            f"- Memory (RSS): median {med_rss:.0f} MB, max **{max_rss:.0f} MB** "
            f"(budget < {RSS_BUDGET_MB} MB)",
            f"- Result: **{'within budget' if ok else 'OVER BUDGET'}**",
            "",
        ]
    )
    print(report)
    if args.out:
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
