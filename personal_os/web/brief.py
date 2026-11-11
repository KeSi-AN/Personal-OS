"""The brief (phase 10): how the last 24 hours went, as a page with charts.

Charts are inline SVG (no chart library, nothing loaded from the internet). Opened in the
browser when capture starts (at most every [brief] min_gap_h hours), and at /brief anytime.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import timedelta
from typing import Any

from personal_os.context import activity
from personal_os.web.html import dur, e
from personal_os.web.v2 import names, shell

COLORS = {
    "building": "#3559d6", "research": "#12a594", "chill": "#e0782b", "gaming": "#9b59d0",
    "communication": "#d4a017", "other": "#8a8f98", "idle": "#c3c7cf",
}  # fmt: skip
LABELS = {
    "building": "Coding & building", "research": "Research & learning", "chill": "Chill",
    "gaming": "Gaming", "communication": "Messages & calls", "other": "Other", "idle": "Idle",
}  # fmt: skip
STATE_FILE = "brief.json"


def donut(by_cat: dict[str, int], size: int = 200) -> str:
    total = sum(by_cat.values())
    if not total:
        return "<p class='muted small'>Nothing recorded.</p>"
    r, cx, cy, w = 70, size / 2, size / 2, 28
    circ = 2 * math.pi * r
    arcs, offset = [], 0.0
    for cat in activity.CATEGORIES:
        v = by_cat.get(cat, 0)
        if not v:
            continue
        frac = v / total
        arcs.append(
            f"<circle r='{r}' cx='{cx}' cy='{cy}' fill='none' stroke='{COLORS[cat]}' stroke-width='{w}' "
            f"stroke-dasharray='{frac * circ:.2f} {circ:.2f}' stroke-dashoffset='{-offset * circ:.2f}' "
            f"transform='rotate(-90 {cx} {cy})'><title>{LABELS[cat]}: {dur(v)}</title></circle>"
        )
        offset += frac
    active = sum(v for k, v in by_cat.items() if k != "idle")
    return (
        f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}' role='img' aria-label='time by category'>"
        + "".join(arcs)
        + f"<text x='{cx}' y='{cy - 4}' text-anchor='middle' font-size='20' font-weight='700' fill='currentColor'>{dur(active)}</text>"
        + f"<text x='{cx}' y='{cy + 16}' text-anchor='middle' font-size='11' fill='currentColor' opacity='.6'>active</text></svg>"
    )


def legend(by_cat: dict[str, int]) -> str:
    total = sum(by_cat.values()) or 1
    rows = []
    for cat in activity.CATEGORIES:
        v = by_cat.get(cat, 0)
        if v:
            rows.append(
                f"<div class='row'><span style='width:12px;height:12px;border-radius:3px;background:{COLORS[cat]};flex:none'></span>"
                f"<span class='grow'>{LABELS[cat]}</span><b>{dur(v)}</b><span class='small muted' style='width:42px;text-align:right'>{round(100 * v / total)}%</span></div>"
            )
    return "".join(rows)


def hours_chart(b: activity.Breakdown, width: int = 720, height: int = 170) -> str:
    start = b.start.astimezone()
    cols = [(start + timedelta(hours=i)).hour for i in range(24)]
    peak = max((sum(b.by_hour.get(h, {}).values()) for h in cols), default=0) or 1
    bw = width / 24
    bars = []
    for i, h in enumerate(cols):
        y = float(height - 18)
        for cat in activity.CATEGORIES:
            v = b.by_hour.get(h, {}).get(cat, 0)
            if not v:
                continue
            hgt = (height - 30) * v / peak
            y -= hgt
            bars.append(
                f"<rect x='{i * bw + 2:.1f}' y='{y:.1f}' width='{bw - 4:.1f}' height='{hgt:.1f}' rx='2' fill='{COLORS[cat]}'>"
                f"<title>{h:02d}:00 {LABELS[cat]}: {dur(v)}</title></rect>"
            )
        if i % 3 == 0:
            bars.append(
                f"<text x='{i * bw + bw / 2:.1f}' y='{height - 4}' text-anchor='middle' font-size='10' fill='currentColor' opacity='.55'>{h:02d}</text>"
            )
    return f"<svg viewBox='0 0 {width} {height}' width='100%' role='img' aria-label='time by hour'>{''.join(bars)}</svg>"


def bars(items: list[tuple[str, int]], color: str = "#3559d6", link: str = "") -> str:
    if not items:
        return "<p class='muted small'>Nothing yet.</p>"
    top = items[0][1] or 1
    out = []
    for label, ms in items:
        name = f"<a href='{link}{e(label)}'>{e(label)}</a>" if link else e(label)
        out.append(
            f"<div class='row'><div style='width:180px' class='clip'>{name}</div>"
            f"<div class='track'><div class='bar' style='width:{max(3, int(100 * ms / top))}%;background:{color}'></div></div>"
            f"<div class='small muted' style='width:64px;text-align:right'>{dur(ms)}</div></div>"
        )
    return "".join(out)


def narrative(b: activity.Breakdown, project_names: dict[str, str]) -> str:
    a = b.active_ms
    if not a:
        return "Nothing was recorded in the last 24 hours."
    parts = []
    build, research, chill = (b.by_category.get(c, 0) for c in ("building", "research", "chill"))
    if b.by_project:
        pid, ms = max(b.by_project.items(), key=lambda kv: kv[1])
        parts.append(f"Most of your project time went to {project_names.get(pid, pid)} ({dur(ms)})")
    if build:
        parts.append(f"{dur(build)} coding and building")
    if research:
        parts.append(f"{dur(research)} researching")
    if chill:
        parts.append(f"{dur(chill)} chilling")
    if b.by_category.get("gaming"):
        parts.append(f"{dur(b.by_category['gaming'])} gaming")
    share = round(100 * b.productive_share)
    verdict = (
        "a strong day"
        if share >= 60
        else "a balanced day"
        if share >= 35
        else "a light day for work"
    )
    return f"{'; '.join(parts)}. {share}% of your active time was productive: {verdict}."


def page(state: Any, conn: sqlite3.Connection, hours: int = 24, *, phone: bool = False) -> str:
    from personal_os.core import modes, runtime
    from personal_os.projects import items

    now = state.clock.now()
    start = now - timedelta(hours=hours)
    cache = activity.load_cache(state.config.data_dir)
    b = activity.breakdown(conn, start, now, cache)
    pn = names(conn)
    share = round(100 * b.productive_share)
    tiles = "".join(
        f"<div class='card' style='padding:12px 14px'><div class='small muted'>{e(label)}</div><div class='big' style='font-size:22px;color:{color}'>{value}</div></div>"
        for label, value, color in [
            ("Active", dur(b.active_ms), "inherit"),
            ("Productive", f"{share}%", COLORS["building"] if share >= 50 else COLORS["chill"]),
            ("Coding & building", dur(b.by_category.get("building", 0)), COLORS["building"]),
            ("Research", dur(b.by_category.get("research", 0)), COLORS["research"]),
            ("Chill", dur(b.by_category.get("chill", 0)), COLORS["chill"]),
            ("Idle", dur(b.by_category.get("idle", 0)), "inherit"),
        ]
    )  # fmt: skip
    projects = [
        (pn.get(p, p), ms) for p, ms in sorted(b.by_project.items(), key=lambda kv: -kv[1])[:6]
    ]
    apps = sorted(b.by_app.items(), key=lambda kv: -kv[1])[:6]
    sites = sorted(b.by_site.items(), key=lambda kv: -kv[1])[:6]
    topics: dict[str, int] = {}
    for r in b.research:
        key = r.get("topic") or r["title"][:50]
        topics[key] = topics.get(key, 0) + r["ms"]
    research_rows = (
        "".join(
            f"<div class='row'><span class='grow clip'>{e(t)}</span><span class='small muted'>{dur(ms)}</span></div>"
            for t, ms in sorted(topics.items(), key=lambda kv: -kv[1])[:8]
        )
        or "<p class='muted small'>No research or learning in this window.</p>"
    )
    extra = runtime.read_json(state.config.runtime_dir / STATE_FILE) or {}
    kept = "".join(
        f"<div class='row'><span class='chip'>{'new' if k.get('created') else '+' + str(k.get('added', 0))}</span>"
        f"<span class='grow'>{e(k.get('project'))}</span><span class='small muted'>{k.get('minutes', 0)} min</span></div>"
        for k in extra.get("kept", [])
    )  # fmt: skip
    flags = "".join(f"<div class='row'>⚑ {e(f)}</div>" for f in activity.flag(b))
    switches = modes.history(state.config.runtime_dir, start)
    mode_line = (", ".join(f"{e(m['mode'])} at {e(m['at'][11:16])}" for m in switches[-6:])
                 or e(modes.current(state.config.runtime_dir)))  # fmt: skip
    todo = "".join(f"<div class='row'>☐ {e(t.title)}</div>" for t in items.todos(conn, limit=5))
    body = (
        f"<h1>Your last {hours} hours</h1><p class='sub'>{e(start.astimezone().strftime('%a %d %b %H:%M'))} – "
        f"{e(now.astimezone().strftime('%a %d %b %H:%M'))}</p>"
        f"<div class='card' style='margin-bottom:16px'>{e(narrative(b, pn))}</div>"
        f"<div class='grid' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:16px'>{tiles}</div>"
        "<div class='grid'>"
        f"<div class='card'><h2>Where the time went</h2><div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap'>{donut(dict(b.by_category))}<div class='grow'>{legend(dict(b.by_category))}</div></div></div>"
        f"<div class='card'><h2>Projects</h2>{bars(projects, COLORS['building'])}</div>"
        f"<div class='card wide'><h2>Hour by hour</h2>{hours_chart(b)}</div>"
        f"<div class='card'><h2>Research & learning</h2>{research_rows}"
        + (f"<h2 style='margin-top:14px'>Kept for later</h2>{kept}" if kept else "")
        + "</div>"
        f"<div class='card'><h2>Top apps</h2>{bars([(a, m) for a, m in apps], COLORS['other'])}<h2 style='margin-top:14px'>Top sites</h2>{bars([(s, m) for s, m in sites], COLORS['research'])}</div>"
        f"<div class='card'><h2>What stood out</h2>{flags or '<p class=\"muted small\">Nothing unusual.</p>'}"
        f"<h2 style='margin-top:14px'>Modes</h2><div class='small'>{mode_line}</div></div>"
        f"<div class='card'><h2>Next up</h2>{todo or '<p class=\"muted small\">No open to-dos.</p>'}</div>"
        "</div>"
    )  # fmt: skip
    return shell("Brief", "/brief", body, phone=phone)


# --- at start -----------------------------------------------------------------------------------


def prepare(cfg: Any, provider: Any, clock: Any) -> dict[str, Any]:
    """Before the brief opens: judge new videos, and keep research that no project claims.
    Saved to runtime/brief.json for the page. Never raises."""
    from personal_os.core import runtime
    from personal_os.projects.research import keep_research
    from personal_os.storage.db import open_db

    out: dict[str, Any] = {"at": clock.now().isoformat(), "kept": []}
    conn = open_db(cfg.db_path)
    try:
        now = clock.now()
        cache = activity.load_cache(cfg.data_dir)
        b = activity.breakdown(conn, now - timedelta(hours=24), now, cache)
        unknown = provider is not None and b.unknown_titles
        if unknown and activity.classify_titles(provider, b.unknown_titles, cache):
            activity.save_cache(cfg.data_dir, cache)
            b = activity.breakdown(conn, now - timedelta(hours=24), now, cache)
        kept = keep_research(conn, clock, b.research, provider)
        out["kept"] = [k.__dict__ for k in kept]
    except Exception as exc:
        out["error"] = type(exc).__name__
    finally:
        conn.close()
    runtime.write_json_atomic(cfg.runtime_dir / STATE_FILE, out)
    return out


def due(cfg: Any, clock: Any) -> bool:
    """Open it now? Not if it was opened within [brief] min_gap_h (restarts, crash loops)."""
    from datetime import datetime

    from personal_os.core import runtime

    if not cfg.brief.on_start:
        return False
    last = (runtime.read_json(cfg.runtime_dir / "brief_opened.json") or {}).get("at")
    if last:
        try:
            if clock.now() - datetime.fromisoformat(last) < timedelta(hours=cfg.brief.min_gap_h):
                return False
        except ValueError:
            pass
    return True


def mark_opened(cfg: Any, clock: Any) -> None:
    from personal_os.core import runtime

    runtime.write_json_atomic(
        cfg.runtime_dir / "brief_opened.json", {"at": clock.now().isoformat()}
    )
