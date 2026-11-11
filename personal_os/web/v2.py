"""Dashboard v2 (phase 9l, docs/DASHBOARD_V2.md): about your day and your projects, not the
system. Same server, same login and protections as before (127.0.0.1 only, one-time login
link, X-POS header and same-origin on changes, strict CSP, no network loads), no build step.

    /               Today: time per project, next up, where you left off, now, timeline, Donna
    /p              Projects: time, last worked on, sources/notes/to-dos counts
    /p/<id>         A project: overview, sources, notes, to-dos, timeline, resume
    /donna          The conversation with Donna, in full; ask her anything
    /notes          Notes and to-dos everywhere, and the unfiled inbox
    /settings       Voice, browser, and the classic privacy/memory/actions pages

The old dashboard's home is at /old until v2 has replaced all of it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from personal_os.core.clock import from_ms, to_ms
from personal_os.web.html import JS, dur, e

# --- design ---------------------------------------------------------------------------------

CSS = """
:root { --bg:#f6f6f3; --panel:#fff; --ink:#1c1c1f; --muted:#6a6a72; --line:#e6e5e0;
  --accent:#3559d6; --accent-soft:#e9eefc; --ok:#1f8a4c; --warn:#a96400; --bad:#c0392b;
  --r:12px; --gap:16px; --font:"Segoe UI", system-ui, -apple-system, sans-serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#121214; --panel:#1b1b1e; --ink:#ececef;
  --muted:#9b9ba4; --line:#2a2a2f; --accent:#8aa4ff; --accent-soft:#232a40; --ok:#4cc483;
  --warn:#f0a63a; --bad:#ff6b5e; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--font); }
.app { display:grid; grid-template-columns:200px 1fr; min-height:100vh; }
.rail { padding:20px 12px; border-right:1px solid var(--line); position:sticky; top:0; height:100vh; }
.rail .brand { font-weight:700; padding:4px 10px 16px; }
.rail a { display:block; padding:8px 10px; border-radius:8px; color:var(--ink); text-decoration:none; }
.rail a.on { background:var(--accent-soft); color:var(--accent); font-weight:600; }
.rail a:hover { background:var(--accent-soft); }
.rail .foot { position:absolute; bottom:16px; left:12px; right:12px; font-size:12px; color:var(--muted); }
main { padding:24px 28px; max-width:1180px; }
h1 { font-size:22px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 20px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:0 0 10px; }
.grid { display:grid; gap:var(--gap); grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); }
.wide { grid-column:1 / -1; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r); padding:16px; }
.row { display:flex; gap:10px; align-items:center; padding:6px 0; border-bottom:1px solid var(--line); }
.row:last-child { border-bottom:none; } .grow { flex:1; min-width:0; }
.muted { color:var(--muted); } .small { font-size:13px; } .big { font-size:28px; font-weight:700; }
.clip { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar { height:8px; border-radius:4px; background:var(--accent); }
.track { background:var(--line); border-radius:4px; height:8px; flex:1; }
a { color:var(--accent); }
button, .btn { font:inherit; font-size:13px; padding:6px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); cursor:pointer; text-decoration:none; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.link { border:none; background:none; color:var(--muted); padding:2px 6px; }
input, textarea, select { font:inherit; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--bg); color:var(--ink); width:100%; }
textarea { min-height:70px; resize:vertical; }
form.inline { display:flex; gap:8px; margin-top:10px; } form.inline input { flex:1; }
.chip { display:inline-block; padding:1px 8px; border-radius:999px; background:var(--accent-soft);
  color:var(--accent); font-size:12px; }
.done { text-decoration:line-through; color:var(--muted); }
.block { display:flex; gap:10px; padding:8px 0; border-bottom:1px solid var(--line); }
.block .t { width:110px; color:var(--muted); font-size:13px; flex:none; }
.msg { padding:10px 12px; border-radius:10px; margin:8px 0; max-width:780px; white-space:pre-wrap; }
.msg.you { background:var(--accent-soft); margin-left:auto; }
.msg.her { background:var(--panel); border:1px solid var(--line); }
.msg .meta { font-size:12px; color:var(--muted); margin-bottom:4px; }
#toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); background:var(--ink);
  color:var(--bg); padding:8px 14px; border-radius:8px; display:none; }
@media (max-width:760px) { .app { grid-template-columns:1fr; } .rail { position:static; height:auto;
  display:flex; flex-wrap:wrap; gap:4px; border-right:none; border-bottom:1px solid var(--line); }
  .rail .brand, .rail .foot { display:none; } main { padding:16px; } }
"""

NAV = [("/", "Today"), ("/brief", "Brief"), ("/p", "Projects"), ("/donna", "Donna"),
       ("/notes", "Notes"), ("/settings", "Settings")]  # fmt: skip
PHONE_PAGES = {"/", "/brief", "/p"}


def shell(title: str, active: str, body: str, *, phone: bool = False) -> str:
    links = "".join(
        f"<a href='{href}' class='{'on' if href == active else ''}'>{e(label)}</a>"
        for href, label in NAV
        if not phone or href in PHONE_PAGES
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)} · Personal OS</title><style>{CSS}</style><script>{JS}</script></head>"
        f"<body><div class='app'><nav class='rail'><div class='brand'>Personal OS</div>{links}"
        "<div class='foot'>Ctrl+K: jump</div></nav>"
        f"<main>{body}</main></div><div id='toast'></div>{KEYS}</body></html>"
    )


KEYS = """<script>
document.addEventListener('keydown', (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k') {
    ev.preventDefault();
    const to = prompt('Go to: today, projects, donna, notes, settings');
    const map = {today:'/', projects:'/p', donna:'/donna', notes:'/notes', settings:'/settings'};
    if (to && map[to.trim().toLowerCase()]) location.href = map[to.trim().toLowerCase()];
  }
});
</script>"""


# --- data -------------------------------------------------------------------------------------


def day_start(now: datetime) -> datetime:
    local = now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def minutes_by_project(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> dict[str | None, int]:
    out: dict[str | None, int] = {}
    for pid, ms in conn.execute(
        "SELECT project_id, SUM(active_ms) FROM sessions WHERE ts_start >= ? AND ts_start < ? "
        "GROUP BY project_id",
        (to_ms(start), to_ms(end)),
    ):
        out[pid] = int(ms or 0)
    return out


def last_worked(conn: sqlite3.Connection) -> dict[str, datetime]:
    return {
        pid: from_ms(ts)
        for pid, ts in conn.execute(
            "SELECT project_id, MAX(ts_end) FROM sessions WHERE project_id IS NOT NULL GROUP BY 1"
        )
    }


def state_note(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    r = conn.execute(
        "SELECT body FROM rollups WHERE kind = 'project_state' AND project_id = ? AND status = 'ok' "
        "ORDER BY ts_end DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    try:
        return json.loads(r[0]) if r else {}
    except ValueError:
        return {}


def _state_html(note: dict[str, Any]) -> str:
    parts = []
    for key, label in (("goal", "Goal"), ("in_progress", "In progress"), ("next_steps", "Next"),
                       ("blockers", "Blocked on")):  # fmt: skip
        val = note.get(key)
        if isinstance(val, list):
            val = "; ".join(
                str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in val[:3]
            )
        if val:
            parts.append(f"<div class='small'><b>{label}:</b> {e(val)}</div>")
    return "".join(parts)


def sessions_between(conn: sqlite3.Connection, start: datetime, end: datetime,
                     project_id: str | None = None) -> list[tuple[datetime, datetime, str | None, int]]:  # fmt: skip
    sql = "SELECT ts_start, ts_end, project_id, active_ms FROM sessions WHERE ts_start >= ? AND ts_start < ?"
    args: list[Any] = [to_ms(start), to_ms(end)]
    if project_id is not None:
        sql += " AND project_id = ?"
        args.append(project_id)
    return [
        (from_ms(a), from_ms(b), p, int(m))
        for a, b, p, m in conn.execute(sql + " ORDER BY ts_start", args)
    ]


def names(conn: sqlite3.Connection) -> dict[str, str]:
    from personal_os.projects.registry import load_projects

    return {p.id: p.name for p in load_projects(conn)}


def _bars(minutes: dict[str | None, int], project_names: dict[str, str]) -> str:
    rows = sorted(((ms, pid) for pid, ms in minutes.items() if ms > 0), reverse=True)[:8]
    if not rows:
        return "<p class='muted small'>No focused time recorded yet today.</p>"
    top = rows[0][0] or 1
    out = []
    for ms, pid in rows:
        name = (
            project_names.get(pid or "", "Not a project")
            if pid and pid != "none"
            else "Not a project"
        )
        link = f"<a href='/p/{e(pid)}'>{e(name)}</a>" if pid and pid != "none" else e(name)
        out.append(
            f"<div class='row'><div style='width:140px' class='clip'>{link}</div>"
            f"<div class='track'><div class='bar' style='width:{max(4, int(100 * ms / top))}%'></div></div>"
            f"<div class='small muted' style='width:64px;text-align:right'>{dur(ms)}</div></div>"
        )
    return "".join(out)


def _todo_row(t: Any, project_names: dict[str, str], *, phone: bool) -> str:
    where = project_names.get(t.project_id or "", "") if t.project_id else "unfiled"
    due = (
        f" <span class='chip'>{e(t.due.astimezone().strftime('%a %H:%M'))}</span>" if t.due else ""
    )
    box = (
        ""
        if phone
        else (
            f"<input type='checkbox' style='width:auto' {'checked' if t.done else ''} "
            f"onchange=\"act('/api/v2/items/{e(t.id)}/done', {{done: this.checked}})\">"
        )
    )
    return (f"<div class='row'>{box}<div class='grow {'done' if t.done else ''}'>{e(t.title)}{due}</div>"
            f"<span class='small muted'>{e(where)}</span></div>")  # fmt: skip


def _ask_box(placeholder: str = "Ask Donna anything…") -> str:
    return f"""<form class='inline' onsubmit="ask(event)"><input id='q' placeholder='{e(placeholder)}' autocomplete='off'>
<button class='primary'>Ask</button></form><div id='answer'></div>
<script>
async function ask(ev) {{
  ev.preventDefault();
  const q = document.getElementById('q'); const box = document.getElementById('answer');
  if (!q.value.trim()) return;
  box.innerHTML = "<p class='muted small'>Thinking…</p>";
  try {{
    const r = await api('/api/v2/chat', {{text: q.value}});
    let html = "<div class='msg her'><div class='meta'>Donna</div>" + esc(r.reply) + "</div>";
    if (r.details) html += "<div class='msg her small'>" + esc(r.details) + "</div>";
    if ((r.trace || []).length) html += "<details class='small'><summary>Show log</summary><div style='font-family:Consolas,monospace;white-space:pre-wrap;color:var(--muted)'>" + r.trace.map(esc).join("<br>") + "</div></details>";
    for (const s of r.steps || []) {{
      html += "<div class='row small'><span class='chip'>" + esc(s.tool) + "</span><span class='grow'>" + esc(s.message) + "</span>";
      if (s.pending) html += "<button onclick=\\"act('/api/actions/approve', {{pending:'" + s.pending + "'}})\\">Approve</button>";
      html += "</div>";
    }}
    box.innerHTML = html; q.value = '';
  }} catch (err) {{ box.innerHTML = ''; }}
}}
</script>"""


# --- pages ------------------------------------------------------------------------------------


def today(state: Any, conn: sqlite3.Connection, *, phone: bool = False) -> str:
    from personal_os.actions import reminders
    from personal_os.projects import items

    now = state.clock.now()
    start = day_start(now)
    pn = names(conn)
    mins = minutes_by_project(conn, start, start + timedelta(days=1))
    total = sum(ms for pid, ms in mins.items() if pid and pid != "none")
    next_rows = [
        f"<div class='row'><span class='chip'>⏰ {e(r.due.astimezone().strftime('%H:%M'))}</span>"
        f"<span class='grow'>{e(r.text)}</span></div>"
        for r in reminders.pending(conn)[:5]
    ] + [_todo_row(t, pn, phone=phone) for t in items.todos(conn, limit=6)]
    worked = last_worked(conn)
    left = ""
    if worked:
        pid, at = max(worked.items(), key=lambda kv: kv[1])
        if pid != "none":
            left = (f"<div class='card wide'><h2>Where you left off</h2><b><a href='/p/{e(pid)}'>"
                    f"{e(pn.get(pid, pid))}</a></b> <span class='muted small'>· "
                    f"{e(at.astimezone().strftime('%a %H:%M'))}</span>{_state_html(state_note(conn, pid))}"
                    + ("" if phone else f"<div style='margin-top:10px'><button onclick=\"act('/api/v2/projects/{e(pid)}/resume', {{}}, false)\">Resume</button></div>")
                    + "</div>")  # fmt: skip
    now_card = ""
    if not phone:
        try:
            from personal_os.context.now import snapshot

            snap = snapshot(state.config, conn)
            front = next((w for w in snap.windows if w.front), None)
            playing = [t for t in snap.tabs if t.audible]
            now_card = ("<div class='card'><h2>Now</h2>"
                        + (f"<div class='clip'>In front: <b>{e(front.app)}</b> · {e(front.title)}</div>" if front else "")
                        + "".join(f"<div class='clip'>▶ {e(t.title)}</div>" for t in playing)
                        + f"<div class='small muted'>{len(snap.windows)} windows · {len(snap.tabs)} tabs</div></div>")  # fmt: skip
        except Exception:
            now_card = ""
    blocks = sessions_between(conn, start, start + timedelta(days=1))
    timeline = (
        "".join(
            f"<div class='block'><div class='t'>{e(a.astimezone().strftime('%H:%M'))}–{e(b.astimezone().strftime('%H:%M'))}</div>"
            f"<div class='grow'>{e(pn.get(p or '', 'Not a project') if p and p != 'none' else 'Not a project')}</div>"
            f"<div class='small muted'>{dur(ms)}</div></div>"
            for a, b, p, ms in blocks[-12:]
        )
        or "<p class='muted small'>Nothing yet today.</p>"
    )
    body = (
        f"<h1>Today</h1><p class='sub'>{e(now.astimezone().strftime('%A %d %B'))} · <a href='/brief'>last 24 hours →</a></p>"
        + ("" if phone else _mode_bar(state))
        + "<div class='grid'>"
        f"<div class='card'><h2>Time</h2><div class='big'>{dur(total)}</div><div class='small muted'>on projects today</div>{_bars(mins, pn)}</div>"
        f"<div class='card'><h2>Next up</h2>{''.join(next_rows) or '<p class=\"muted small\">Nothing scheduled.</p>'}</div>"
        f"{now_card}{left}"
        + ("" if phone else f"<div class='card wide'><h2>Donna</h2>{_ask_box()}</div>")
        + f"<div class='card wide'><h2>Timeline</h2>{timeline}</div></div>"
    )  # fmt: skip
    return shell("Today", "/", body, phone=phone)


def _mode_bar(state: Any) -> str:
    from personal_os.core import modes

    st = modes.state(state.config.runtime_dir)
    chips = "".join(
        f"<button class='{'primary' if m == st['mode'] else ''}' title='{e(modes.EFFECTS[m].describe)}' "
        f"onclick=\"act('/api/v2/mode', {{mode:'{m}'}})\">{e(modes.EFFECTS[m].label)}</button>"
        for m in modes.MODES
    )
    auto = " <span class='small muted'>(automatic)</span>" if st.get("by") == "auto" else ""
    return (f"<div class='row' style='border:none;margin:-8px 0 12px'><span class='small muted'>Mode:</span>{chips}{auto}"
            f"<span class='small muted grow'>{e(modes.EFFECTS[st['mode']].describe)}</span></div>")  # fmt: skip


def projects_page(state: Any, conn: sqlite3.Connection, *, phone: bool = False) -> str:
    from personal_os.projects import items
    from personal_os.projects.registry import load_projects

    now = state.clock.now()
    week = minutes_by_project(conn, day_start(now) - timedelta(days=6), now + timedelta(minutes=1))
    worked = last_worked(conn)
    counts = items.counts(conn)
    rows: list[str] = []
    quiet: list[str] = []
    recent = now - timedelta(days=30)
    for p in sorted(
        load_projects(conn),
        key=lambda p: worked.get(p.id, datetime.min.replace(tzinfo=now.tzinfo)),
        reverse=True,
    ):
        if p.id == "none":
            continue
        c = counts.get(p.id, {})
        last = worked.get(p.id)
        active = (last is not None and last >= recent) or bool(c)
        (rows if active else quiet).append(
            f"<div class='row'><div class='grow'><a href='/p/{e(p.id)}'><b>{e(p.name)}</b></a>"
            f"<div class='small muted'>{e('last worked ' + last.astimezone().strftime('%a %d %b') if last else 'not worked on yet')}</div></div>"
            f"<span class='small muted'>{dur(week.get(p.id, 0))} this week</span>"
            f"<span class='chip'>{c.get('sources', 0)} sources</span><span class='chip'>{c.get('notes', 0)} notes</span>"
            f"<span class='chip'>{c.get('todos', 0)} to-dos</span></div>"
        )  # fmt: skip
    create = (
        ""
        if phone
        else (
            "<form class='inline' onsubmit=\"event.preventDefault(); act('/api/v2/projects', {name: this.n.value})\">"
            "<input name='n' placeholder='New project name'><button>Create</button></form>"
        )
    )
    body = (f"<h1>Projects</h1><p class='sub'>Time, sources, notes and to-dos for each.</p>"
            f"<div class='card'>{''.join(rows) or '<p class=\"muted\">No recent projects.</p>'}"
            + (f"<details style='margin-top:10px'><summary class='muted small'>Other projects ({len(quiet)}): "
               f"not worked on in 30 days</summary>{''.join(quiet)}</details>" if quiet else "")
            + f"{create}</div>")  # fmt: skip
    return shell("Projects", "/p", body, phone=phone)


def project_page(
    state: Any, conn: sqlite3.Connection, project_id: str, *, phone: bool = False
) -> str:
    from personal_os.projects import items
    from personal_os.projects.registry import get_project

    p = get_project(conn, project_id)
    if p is None:
        raise HTTPException(404, "no such project")
    now = state.clock.now()
    today_ms = minutes_by_project(conn, day_start(now), now + timedelta(minutes=1)).get(p.id, 0)
    week_ms = minutes_by_project(
        conn, day_start(now) - timedelta(days=6), now + timedelta(minutes=1)
    ).get(p.id, 0)
    last = last_worked(conn).get(p.id)
    sources = items.items(conn, p.id, ("link", "tab", "file"))
    notes = items.items(conn, p.id, ("note",), limit=30)
    todos = items.items(conn, p.id, ("todo",), limit=50)
    pid = e(p.id)

    def src(i: Any) -> str:
        label = e(i.title or i.url)
        link = (
            f"<a href='{e(i.url)}' target='_blank' rel='noreferrer'>{label}</a>"
            if i.url.startswith("http")
            else label
        )
        grp = f" <span class='chip'>{e(i.grp)}</span>" if i.grp else ""
        rm = (
            ""
            if phone
            else f"<button class='link' onclick=\"act('/api/v2/items/{e(i.id)}/delete', {{}})\">remove</button>"
        )
        note = f"<div class='small muted'>{e(i.body)}</div>" if i.body else ""
        return f"<div class='row'><div class='grow clip'>{link}{grp}{note}</div>{rm}</div>"

    add_src = "" if phone else (
        f"<form class='inline' onsubmit=\"event.preventDefault(); act('/api/v2/items', {{project_id:'{pid}', kind:'link', url:this.u.value, title:this.t.value}})\">"
        "<input name='u' placeholder='https://…'><input name='t' placeholder='Title (optional)'><button>Add link</button></form>"
        "<p class='small muted'>Or say “Donna, add this tab group to " + e(p.name) + "”, right-click a page → "
        "<i>Add to Personal OS project</i>, or name a Brave tab group “" + e(p.name) + "”.</p>"
    )  # fmt: skip
    add_note = "" if phone else (
        f"<form onsubmit=\"event.preventDefault(); act('/api/v2/items', {{project_id:'{pid}', kind:'note', body:this.b.value}})\">"
        "<textarea name='b' placeholder='Write a note…'></textarea><button style='margin-top:8px'>Save note</button></form>"
    )  # fmt: skip
    add_todo = "" if phone else (
        f"<form class='inline' onsubmit=\"event.preventDefault(); act('/api/v2/items', {{project_id:'{pid}', kind:'todo', title:this.t.value}})\">"
        "<input name='t' placeholder='Add a to-do'><button>Add</button></form>"
    )  # fmt: skip
    note_rows = "".join(
        f"<div class='row'><div class='grow' style='white-space:pre-wrap'>{e(n.body)}"
        f"<div class='small muted'>{e(n.created.astimezone().strftime('%d %b %H:%M'))}</div></div>"
        + ("" if phone else f"<button class='link' onclick=\"act('/api/v2/items/{e(n.id)}/delete', {{}})\">delete</button>")
        + "</div>"
        for n in notes
    )  # fmt: skip
    pn = {p.id: p.name}
    blocks = sessions_between(
        conn, day_start(now) - timedelta(days=6), now + timedelta(minutes=1), p.id
    )
    timeline = "".join(
        f"<div class='block'><div class='t'>{e(a.astimezone().strftime('%a %H:%M'))}</div><div class='grow small'>"
        f"{e(a.astimezone().strftime('%H:%M'))}–{e(b.astimezone().strftime('%H:%M'))}</div><div class='small muted'>{dur(ms)}</div></div>"
        for a, b, _p, ms in blocks[-15:]
    ) or "<p class='muted small'>No sessions this week.</p>"  # fmt: skip
    resume = (
        ""
        if phone
        else f"<button class='primary' onclick=\"act('/api/v2/projects/{pid}/resume', {{}}, false)\">Resume</button>"
    )
    body = (
        f"<p class='small'><a href='/p'>← Projects</a></p><div class='row' style='border:none'>"
        f"<div class='grow'><h1>{e(p.name)}</h1></div>{resume}</div>"
        f"<p class='sub'>{e('Last worked ' + last.astimezone().strftime('%a %d %b, %H:%M') if last else 'Not worked on yet')}</p>"
        "<div class='grid'>"
        f"<div class='card'><h2>Time</h2><div class='big'>{dur(week_ms)}</div><div class='small muted'>this week · {dur(today_ms)} today</div>"
        f"{_state_html(state_note(conn, p.id))}</div>"
        f"<div class='card'><h2>To-dos</h2>{''.join(_todo_row(t, pn, phone=phone) for t in todos) or '<p class=\"muted small\">None yet.</p>'}{add_todo}</div>"
        f"<div class='card wide'><h2>Sources</h2>{''.join(src(i) for i in sources) or '<p class=\"muted small\">No sources yet.</p>'}{add_src}</div>"
        f"<div class='card'><h2>Notes</h2>{add_note}{note_rows}</div>"
        f"<div class='card'><h2>This week</h2>{timeline}</div></div>"
    )  # fmt: skip
    return shell(p.name, "/p", body, phone=phone)


def app_log(data_dir: Any, last: int = 3000) -> list[dict[str, Any]]:
    """Warnings and errors from the app log (it holds no content: names and counts only)."""
    out: list[dict[str, Any]] = []
    path = data_dir / "logs" / "personal_os.log"
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()[-last:]
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("level") in ("WARNING", "ERROR", "CRITICAL"):
            try:
                rec["_at"] = datetime.fromisoformat(str(rec.get("ts")))
            except ValueError:
                continue
            out.append(rec)
    return out


def turn_log(x: dict[str, Any], log: list[dict[str, Any]]) -> str:
    """What happened in one exchange, for "Show log"."""
    rows = []
    times = ", ".join(
        f"{k[:-2]} {x[k]} s" for k in ("listen_s", "handle_s", "speak_s", "total_s") if k in x
    )
    rows.append(
        f"route: {x.get('route', '?')} · via {x.get('trigger', '?')}"
        + (f" · {times}" if times else "")
    )
    if x.get("asked"):
        a = x["asked"]
        rows.append(
            f"asked: {a.get('question', '')} · you: {a.get('answer', '')} · read as: {a.get('understood', '')}"
        )
    rows += [str(t) for t in x.get("trace") or []]
    if x.get("error"):
        rows.append(f"error: {x['error']}")
    try:
        start = datetime.fromisoformat(str(x.get("at")))
        end = start + timedelta(seconds=float(x.get("total_s") or 60) + 10)
        for rec in log:
            if start - timedelta(seconds=5) <= rec["_at"] <= end:
                extra = {
                    k: v for k, v in rec.items() if k not in ("ts", "level", "logger", "msg", "_at")
                }
                rows.append(
                    f"log {rec['level'].lower()}: {rec.get('logger', '')}: {rec.get('msg', '')} {extra or ''}".rstrip()
                )
    except (TypeError, ValueError):
        pass
    body = "".join(f"<div>{e(r)}</div>" for r in rows)
    return (f"<details class='small' style='margin-top:6px'><summary>Show log</summary>"
            f"<div style='font-family:Consolas,monospace;white-space:pre-wrap;color:var(--muted);margin-top:4px'>{body}</div></details>")  # fmt: skip


def donna_page(state: Any, conn: sqlite3.Connection) -> str:
    from personal_os.voice.main import history, local_time

    entries = [x for x in history(state.config.runtime_dir, 60) if x.get("heard")]
    log = app_log(state.config.data_dir)
    msgs = []
    for x in entries[-40:]:
        when = e(local_time(str(x.get("at", "")), "%d %b %H:%M"))
        how = e(x.get("trigger") or "")
        msgs.append(
            f"<div class='msg you'><div class='meta'>You · {how} · {when}</div>{e(x.get('heard'))}</div>"
        )
        if x.get("reply"):
            msgs.append(
                f"<div class='msg her'><div class='meta'>Donna</div>{e(x.get('reply'))}{turn_log(x, log)}</div>"
            )
        else:
            what = (
                "(something went wrong)"
                if x.get("error")
                else f"({e(x.get('route') or 'no reply')})"
            )
            msgs.append(f"<div class='msg her muted'>{what}{turn_log(x, log)}</div>")
    body = (
        "<h1>Donna</h1><p class='sub'>Your conversation, by voice and typed, with every answer in full.</p>"
        f"<div class='card'>{''.join(msgs) or '<p class=\"muted\">Nothing yet. Ask below, or press Ctrl+Alt+Shift+V.</p>'}</div>"
        f"<div class='card' style='margin-top:16px'>{_ask_box('Ask or tell Donna something…')}</div>"
        "<script>window.scrollTo(0, document.body.scrollHeight);</script>"
    )  # fmt: skip
    return shell("Donna", "/donna", body)


def notes_page(state: Any, conn: sqlite3.Connection) -> str:
    from personal_os.projects import items
    from personal_os.projects.registry import load_projects

    pn = names(conn)
    inbox = items.items(conn, None)
    options = "".join(
        f"<option value='{e(p.id)}'>{e(p.name)}</option>"
        for p in load_projects(conn)
        if p.id != "none"
    )

    def unfiled(i: Any) -> str:
        what = e(i.title or i.url or i.body[:80])
        link = (
            f"<a href='{e(i.url)}' target='_blank' rel='noreferrer'>{what}</a>"
            if i.url.startswith("http")
            else what
        )
        return (f"<div class='row'><span class='chip'>{e(i.kind)}</span><div class='grow clip'>{link}</div>"
                f"<select style='width:180px' onchange=\"act('/api/v2/items/{e(i.id)}/move', {{project_id:this.value}})\">"
                f"<option value=''>File into…</option>{options}</select>"
                f"<button class='link' onclick=\"act('/api/v2/items/{e(i.id)}/delete', {{}})\">delete</button></div>")  # fmt: skip

    recent = conn.execute(
        "SELECT id FROM project_items WHERE kind = 'note' ORDER BY created_at DESC LIMIT 40"
    ).fetchall()
    note_rows = "".join(
        f"<div class='row'><div class='grow' style='white-space:pre-wrap'>{e(n.body)}<div class='small muted'>"
        f"{e(pn.get(n.project_id or '', 'unfiled'))} · {e(n.created.astimezone().strftime('%d %b %H:%M'))}</div></div></div>"
        for r in recent if (n := items.get(conn, r[0])) is not None
    )  # fmt: skip
    body = (
        "<h1>Notes</h1><p class='sub'>Notes and to-dos from every project, and things not filed yet.</p><div class='grid'>"
        "<div class='card'><h2>Quick note</h2><form onsubmit=\"event.preventDefault(); act('/api/v2/items', {project_id:null, kind:'note', body:this.b.value})\">"
        "<textarea name='b' placeholder='Note it down…'></textarea><button style='margin-top:8px'>Save</button></form></div>"
        "<div class='card'><h2>To-dos</h2>"
        + "".join(_todo_row(t, pn, phone=False) for t in items.todos(conn, limit=40))
        + "<form class='inline' onsubmit=\"event.preventDefault(); act('/api/v2/items', {project_id:null, kind:'todo', title:this.t.value})\">"
        "<input name='t' placeholder='Add a to-do'><button>Add</button></form></div>"
        f"<div class='card wide'><h2>Unfiled</h2>{''.join(unfiled(i) for i in inbox) or '<p class=\"muted small\">Nothing waiting. Right-click a page in Brave → <i>Add to Personal OS project</i>.</p>'}</div>"
        f"<div class='card wide'><h2>All notes</h2>{note_rows or '<p class=\"muted small\">No notes yet.</p>'}</div></div>"
    )  # fmt: skip
    return shell("Notes", "/notes", body)


SETTINGS = {
    "voice.wake_word_enabled": ("Wake word", "Listen for “Donna” without the hotkey", "bool"),
    "voice.popup": ("Popup", "Show each turn and full answers in a small window", "bool"),
    "voice.followup_s": (
        "Follow-up window",
        "Seconds to keep listening after a reply (0 = off)",
        "number",
    ),
    "voice.enabled": ("Voice", "Push-to-talk and spoken replies", "bool"),
}


def settings_page(state: Any, conn: sqlite3.Connection) -> str:
    from personal_os.browser import control
    from personal_os.config.loader import load_config

    cfg = load_config(state.home) if state.home else state.config
    rows = []
    for key, (label, hint, kind) in SETTINGS.items():
        section, name = key.split(".")
        value = getattr(getattr(cfg, section), name)
        if kind == "bool":
            ctrl = (f"<input type='checkbox' style='width:auto' {'checked' if value else ''} "
                    f"onchange=\"act('/api/v2/setting', {{key:'{key}', value:this.checked}}, false)\">")  # fmt: skip
        else:
            ctrl = (f"<input type='number' style='width:90px' value='{e(value)}' "
                    f"onchange=\"act('/api/v2/setting', {{key:'{key}', value:Number(this.value)}}, false)\">")  # fmt: skip
        rows.append(
            f"<div class='row'><div class='grow'><b>{e(label)}</b><div class='small muted'>{e(hint)}</div></div>{ctrl}</div>"
        )
    browsers = control.connected(state.config.runtime_dir)
    classic = "".join(
        f"<div class='row'><a class='grow' href='{href}'>{e(label)}</a><span class='small muted'>{e(hint)}</span></div>"
        for href, label, hint in [
            ("/privacy", "Privacy and sources", "what's captured, pause, forget, export, phone access"),
            ("/memory", "Memory", "facts you asked Donna to remember"),
            ("/actions", "Actions", "the action log, undo, approvals"),
            ("/inbox", "Activity inbox", "activity the tracker couldn't place"),
            ("/timeline", "Timeline (classic)", "every session in detail"),
            ("/old", "Old dashboard", "the previous home page"),
        ]
    )  # fmt: skip
    body = (
        "<h1>Settings</h1><p class='sub'>Changes apply within a few seconds.</p><div class='grid'>"
        f"<div class='card'><h2>Voice</h2>{''.join(rows)}</div>"
        f"<div class='card'><h2>Browser</h2><div>{e(', '.join(browsers) + ' connected' if browsers else 'No browser connected')}</div>"
        "<div class='small muted'>Donna can open, play, read and (with your yes) click in it.</div></div>"
        f"<div class='card wide'><h2>Privacy, memory and more</h2>{classic}</div></div>"
    )  # fmt: skip
    return shell("Settings", "/settings", body)


# --- api --------------------------------------------------------------------------------------


class ItemBody(BaseModel):
    project_id: str | None = None
    kind: str = Field(pattern="^(link|note|todo)$")
    title: str = Field("", max_length=300)
    url: str = Field("", max_length=2000)
    body: str = Field("", max_length=5000)


class DoneBody(BaseModel):
    done: bool = True


class MoveBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)


class ProjectBody(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class ChatBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ModeBody(BaseModel):
    mode: str = Field(min_length=2, max_length=20)


class SettingBody(BaseModel):
    key: str
    value: bool | float | int


def register(
    app: FastAPI,
    state: Any,
    executor: Callable[[sqlite3.Connection, Callable[[str], bool]], Any],
    defer: Callable[[str], bool],
    *,
    lan: bool = False,
) -> None:
    """The v2 pages and their endpoints, on the app the old dashboard is built on."""

    @app.get("/", response_class=HTMLResponse)
    def v2_today() -> str:
        with state.db() as conn:
            return today(state, conn, phone=lan)

    @app.get("/p", response_class=HTMLResponse)
    def v2_projects() -> str:
        with state.db() as conn:
            return projects_page(state, conn, phone=lan)

    @app.get("/p/{project_id}", response_class=HTMLResponse)
    def v2_project(project_id: str) -> str:
        with state.db() as conn:
            return project_page(state, conn, project_id, phone=lan)

    @app.get("/donna", response_class=HTMLResponse)
    def v2_donna() -> str:
        with state.db() as conn:
            return donna_page(state, conn)

    @app.get("/notes", response_class=HTMLResponse)
    def v2_notes() -> str:
        with state.db() as conn:
            return notes_page(state, conn)

    @app.get("/brief", response_class=HTMLResponse)
    def v2_brief(h: int = 24) -> str:
        from personal_os.web import brief

        with state.db() as conn:
            return brief.page(state, conn, max(1, min(h, 24 * 7)), phone=lan)

    @app.post("/api/v2/mode")
    def v2_mode(body: ModeBody) -> dict[str, Any]:
        from personal_os.core import modes

        mode = modes.normalize(body.mode)
        if mode is None:
            raise HTTPException(400, "the modes are work, gaming and chill")
        modes.set_mode(state.config.runtime_dir, mode, by="dashboard", now=state.clock.now())
        return {"message": f"{modes.EFFECTS[mode].label} mode."}

    @app.get("/settings", response_class=HTMLResponse)
    def v2_settings() -> str:
        with state.db() as conn:
            return settings_page(state, conn)

    @app.post("/api/v2/items")
    def v2_add(body: ItemBody) -> dict[str, Any]:
        from personal_os.projects import items
        from personal_os.projects.registry import get_project

        if body.kind == "link" and not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "a link needs an http(s) address")
        if body.kind == "note" and not body.body.strip():
            raise HTTPException(400, "write something first")
        if body.kind == "todo" and not body.title.strip():
            raise HTTPException(400, "say what to do")
        with state.db() as conn:
            if body.project_id and get_project(conn, body.project_id) is None:
                raise HTTPException(404, "no such project")
            items.add(conn, state.clock, body.project_id or None, body.kind,  # type: ignore[arg-type]
                      title=body.title or body.body[:80], url=body.url, body=body.body,
                      source="dashboard")  # fmt: skip
        return {
            "message": {"link": "Link added.", "note": "Note saved.", "todo": "To-do added."}[
                body.kind
            ]
        }

    @app.post("/api/v2/items/{item_id}/done")
    def v2_done(item_id: str, body: DoneBody) -> dict[str, Any]:
        from personal_os.projects import items

        with state.db() as conn:
            if not items.set_done(conn, state.clock, item_id, body.done):
                raise HTTPException(404, "no such to-do")
        return {"message": "Done." if body.done else "Reopened."}

    @app.post("/api/v2/items/{item_id}/move")
    def v2_move(item_id: str, body: MoveBody) -> dict[str, Any]:
        from personal_os.projects import items
        from personal_os.projects.registry import get_project

        with state.db() as conn:
            p = get_project(conn, body.project_id)
            item = items.get(conn, item_id)
            if p is None or item is None:
                raise HTTPException(404, "not found")
            items.update(conn, state.clock, item_id, project_id=p.id)
            if item.url and item.kind in ("link", "tab"):
                items.learn_from(conn, state.clock, item.url, p.id)
        return {"message": f"Filed into {p.name}."}

    @app.post("/api/v2/items/{item_id}/delete")
    def v2_delete(item_id: str) -> dict[str, Any]:
        from personal_os.projects import items

        with state.db() as conn:
            if not items.delete(conn, item_id):
                raise HTTPException(404, "not found")
        return {"message": "Removed."}

    @app.post("/api/v2/projects")
    def v2_create(body: ProjectBody) -> dict[str, Any]:
        from personal_os.projects.registry import add_project

        with state.db() as conn:
            p = add_project(conn, state.clock, body.name.strip(), [], [])
        return {"message": f"Created {p.name}.", "id": p.id}

    @app.post("/api/v2/projects/{project_id}/resume")
    def v2_resume(project_id: str) -> dict[str, Any]:
        from personal_os.actions.base import Origin

        with state.db() as conn:
            out = executor(conn, defer).request("project_resume", {"project": project_id},
                                                origin=Origin.USER, request_text="resume (dashboard)")  # fmt: skip
        if not out.ok:
            raise HTTPException(409, str(out.message))
        return {"message": out.message}

    @app.post("/api/v2/chat")
    def v2_chat(body: ChatBody) -> dict[str, Any]:
        from personal_os.actions import agent
        from personal_os.actions.audit import history as audit_history
        from personal_os.web.app import PendingAction

        provider = state.provider()
        if provider is None:
            raise HTTPException(503, "the local model isn't available")
        with state.db() as conn:
            ex = executor(conn, defer)
            r = agent.chat_turn(body.text, ex, provider, source="dashboard")
            steps = []
            for out in r.outcomes:
                step: dict[str, Any] = {
                    "tool": out.tool,
                    "status": out.status,
                    "message": out.message,
                }
                if out.status == "pending":
                    first = audit_history(conn, out.action_id)[0]
                    pid = out.action_id[-12:]
                    state.pending[pid] = PendingAction(first.tool, first.args, first.tainted, body.text,
                                                       out.preview, first.tier, state.clock.now())  # fmt: skip
                    step["pending"] = pid
                steps.append(step)
        return {"reply": r.reply, "details": r.details, "steps": steps, "trace": r.trace}

    @app.post("/api/v2/setting")
    def v2_setting(body: SettingBody) -> dict[str, Any]:
        from personal_os.config.loader import set_value

        if body.key not in SETTINGS:
            raise HTTPException(400, "not a setting this page changes")
        kind = SETTINGS[body.key][2]
        value: Any = bool(body.value) if kind == "bool" else float(body.value)
        if kind == "number" and not 0 <= value <= 20:
            raise HTTPException(400, "between 0 and 20")
        set_value(body.key, value, state.home)
        return {"message": f"{SETTINGS[body.key][0]}: saved."}
