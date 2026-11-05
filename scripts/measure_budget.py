"""Measure the running daemon's CPU and memory against the budget (DESIGN.md §4.12).

    uv run python scripts/measure_budget.py --minutes 60 [--out docs/BUDGET.md]

    uv run python scripts/measure_budget.py --popup 60 [--out docs/BUDGET.md]

Samples the daemon process (found via the health file in the data dir) every 5 s.
CPU is reported as a percentage of the whole machine (all cores), like Task Manager.

--popup SECONDS opens the voice popup (the demo, pinned and looping, so its animations run),
samples it with its WebView2 child processes every second, closes it, and checks that every
one of those processes is gone: the popup costs nothing while closed.
"""

from __future__ import annotations

import argparse
import contextlib
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

from personal_os.config.loader import load_config
from personal_os.config.schema import Config
from personal_os.core import runtime

CPU_BUDGET = 2.0
RSS_BUDGET_MB = 300


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--out", type=Path, help="Append a Markdown result section to this file.")
    ap.add_argument("--popup", type=float, metavar="SECONDS", help="Measure the voice popup.")
    args = ap.parse_args()

    cfg = load_config()
    if args.popup:
        return measure_popup(cfg, args.popup, args.out)
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


def _tree(proc: psutil.Process) -> list[psutil.Process]:
    try:
        return [proc, *proc.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def measure_popup(cfg: Config, seconds: float, out: Path | None) -> int:
    from personal_os.core import spawn
    from personal_os.interfaces import popup

    if popup.is_open(cfg.runtime_dir):
        print("A popup is open already; close it first.", file=sys.stderr)
        return 1
    reason = popup.unavailable_reason()
    if reason:
        print(f"The popup can't open: {reason}", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    host = subprocess.Popen(
        spawn.module_command("personal_os.interfaces.popup", "--demo", "--pinned"),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
    )
    proc = psutil.Process(host.pid)
    startup = 0.0
    while not startup and time.monotonic() - t0 < 30 and host.poll() is None:
        for child in _tree(proc)[1:]:
            with contextlib.suppress(psutil.Error):
                if "--type=renderer" in " ".join(child.cmdline()):
                    startup = time.monotonic() - t0  # the page's renderer is up
        time.sleep(0.05)
    time.sleep(3)  # let it settle before sampling
    ncpu = psutil.cpu_count() or 1
    seen: dict[int, psutil.Process] = {}
    cpu: list[float] = []
    rss: list[float] = []
    uss: list[float] = []
    nproc = 0
    end = time.monotonic() + seconds
    print(f"Sampling the popup (pid {host.pid}) and its WebView2 processes for {seconds:g} s...")
    while time.monotonic() < end and host.poll() is None:
        for p in _tree(proc):
            if p.pid not in seen:
                seen[p.pid] = p
                p.cpu_percent(None)
        time.sleep(1)
        c = r = u = 0.0
        alive = 0
        for p in list(seen.values()):
            try:
                c += p.cpu_percent(None)
                mem = p.memory_full_info()
                r += mem.rss / 1e6
                u += mem.uss / 1e6
                alive += 1
            except psutil.NoSuchProcess:
                del seen[p.pid]
        cpu.append(c / ncpu)
        rss.append(r)
        uss.append(u)
        nproc = max(nproc, alive)
    pids = list(seen)
    for p in reversed(_tree(proc)):
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(psutil.pid_exists(pid) for pid in pids):
        time.sleep(0.2)
    left = [pid for pid in pids if psutil.pid_exists(pid)]
    if not cpu:
        print("The popup exited before it could be measured.", file=sys.stderr)
        return 1
    started = datetime.now().astimezone()
    closed = "every popup process exited" if not left else f"{len(left)} still running"
    report = "\n".join(
        [
            f"### Voice popup, {started:%Y-%m-%d %H:%M}: {len(cpu)} samples over {seconds:g} s",
            "",
            f"- Open (demo, pinned, animating): {nproc} processes (Python host + WebView2)",
            f"- Renderer up {startup:.1f} s after the spawn (sampling began 3 s later)",
            f"- CPU (% of machine): median **{statistics.median(cpu):.2f}%**, max {max(cpu):.2f}%",
            f"- Memory: private (USS) median **{statistics.median(uss):.0f} MB**, max "
            f"{max(uss):.0f} MB; RSS sum max {max(rss):.0f} MB (counts shared pages twice)",
            f"- Closed: {closed}",
            "",
        ]
    )
    print(report)
    if out:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
