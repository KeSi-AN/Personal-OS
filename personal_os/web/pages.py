"""The dashboard's pages. Each reads through the same functions the CLI uses."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from personal_os.actions import killswitch
from personal_os.actions.audit import latest
from personal_os.actions.tools import build_registry
from personal_os.context import memory, rollups
from personal_os.context.evidence import fetch_by_ids, finalize, lines_from_rows
from personal_os.context.summarizer import StoredSummary, load_summaries
from personal_os.core import pause as pause_mod
from personal_os.core import runtime
from personal_os.core.clock import to_ms
from personal_os.projects.registry import NONE_PROJECT, load_projects
from personal_os.projects.sessions import Session, day_bounds, load_sessions
from personal_os.storage import maintenance, repo
from personal_os.web import auth
from personal_os.web.html import card, dur, e, page, pills, when

if TYPE_CHECKING:
    from personal_os.context.ask import Answer
    from personal_os.web.app import WebState


def _status(state: WebState, conn: sqlite3.Connection) -> str:
    paused = pause_mod.is_paused(conn, state.clock)
    killed = killswitch.engaged(state.config.runtime_dir)
    cap = '<span class="warn">paused</span>' if paused else '<span class="ok">capturing</span>'
    act = ' · <span class="bad">actions off</span>' if killed else ""
    return f'<span class="small">{cap}{act}</span>'


def login_needed(*, expired: bool = False) -> str:
    msg = "That link has expired or was already used." if expired else "You're not signed in."
    body = card(
        f"<p>{msg}</p><p>Open the dashboard from the tray icon (<b>Open dashboard</b>) or run "
        "<code>personal-os dashboard</code>: each link signs you in once.</p>"
    )
    return page("Sign in", body, path="")


def pair_form(*, error: bool = False) -> str:
    err = '<p class="bad">That code didn\'t work. Get a new one on the laptop.</p>' if error else ""
    body = card(
        f"""{err}<p>On the laptop: Privacy → Phone access → <b>Show pairing code</b>.</p>
<form method="post" action="/pair"><div class="row">
<input name="code" inputmode="numeric" pattern="[0-9]{{6}}" placeholder="6-digit code" required>
<input name="name" placeholder="This phone's name" value="phone">
<button class="primary">Pair</button></div></form>"""
    )
    return page("Pair this phone", body, path="", phone=True)


# --- shared bits ------------------------------------------------------------------------------


def _summary_block(s: StoredSummary | None) -> str:
    if s is None:
        return ""
    b = s.body
    out = f"<div><b>{e(b.goal)}</b> <span class='muted small'>({e(b.goal_confidence)})</span></div>"
    if b.actions:
        out += "<ul>" + "".join(f"<li>{e(i.text)}</li>" for i in b.actions) + "</ul>"
    if b.open_threads:
        out += (
            "<div class='small warn'>Open: "
            + e("; ".join(i.text for i in b.open_threads))
            + "</div>"
        )
    if b.next_step:
        out += f"<div class='small'>Next: {e(b.next_step.text)}</div>"
    return out


def _session_card(
    conn: sqlite3.Connection, s: Session, summary: StoredSummary | None, *, evidence: bool = True
) -> str:
    st = s.stats
    head = (
        f"<div class='row'><b>{when(s.start, '%H:%M')}–{when(s.end, '%H:%M')}</b>"
        f"<span class='pill'>{e(s.project_id or 'unassigned')}</span>"
        f"<span class='muted small'>active {dur(s.active_ms)}</span></div>"
    )
    body = _summary_block(summary) or "<div class='muted small'>Not summarised yet.</div>"
    facts = []
    if st.get("apps"):
        facts.append("apps " + ", ".join(list(st["apps"])[:4]))
    if st.get("files"):
        facts.append("files " + ", ".join(list(st["files"])[:5]))
    if st.get("commits"):
        facts.append("commits: " + "; ".join(st["commits"][:3]))
    if st.get("commands"):
        facts.append(f"{st['commands']} commands ({st.get('failed_commands', 0)} failed)")
    detail = f"<div class='small muted'>{e(' · '.join(facts))}</div>" if facts else ""
    ev = ""
    if evidence and summary is not None and summary.evidence_event_ids:
        lines = finalize(lines_from_rows(fetch_by_ids(conn, summary.evidence_event_ids[:30])), 30)
        items = "".join(f"<div class='evidence'>{e(x.render())}</div>" for x in lines.lines)
        ev = f"<details><summary class='small'>Evidence ({len(lines.lines)})</summary>{items}</details>"
    return card(head + body + detail + ev)


def _summaries(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> dict[str | None, StoredSummary]:
    return {s.session_id: s for s in load_summaries(conn, start, end)}


# --- pages -------------------------------------------------------------------------------------


def today(state: WebState, conn: sqlite3.Connection, *, phone: bool = False) -> str:
    now = state.clock.now()
    start, end = day_bounds(now)
    sessions = load_sessions(conn, start, end)
    summaries = _summaries(conn, start, end)
    paused = pause_mod.is_paused(conn, state.clock)
    toggle = (
        '<button class="primary" onclick="act(\'/api/resume\')">Resume capture</button>'
        if paused
        else "<button onclick=\"act('/api/pause',{minutes:30})\">Pause 30 min</button>"
        "<button onclick=\"act('/api/pause')\">Pause</button>"
    )
    controls = card(
        f"<div class='row'>{_status(state, conn)}<span class='grow'></span>{toggle}"
        "<button onclick=\"act('/api/forget',{minutes:15})\">Forget last 15 min</button></div>"
    )
    ask_box = card(
        "<form class='row' action='/ask'><input type='text' name='q' class='grow' "
        "placeholder='Ask about your work: where did I leave off on …?'>"
        "<button class='primary'>Ask</button></form>"
    )
    total = sum(s.active_ms for s in sessions)
    inbox_n = conn.execute(
        "SELECT count(*) FROM events WHERE assign_source = 'unassigned' AND ts_start >= ?",
        (to_ms(now - timedelta(days=7)),),
    ).fetchone()[0]
    extra = "" if phone else f" · <a href='/inbox'>{inbox_n} unassigned in the inbox</a>"
    welcome = ""
    if not phone and not repo.get_setting(conn, "setup.reviewed", False):
        welcome = card(
            "<b>Welcome.</b> Before relying on this, look at what each source captures and turn "
            "off anything you don't want: <a href='/privacy'>Privacy → Sources</a>. "
            "<button onclick=\"act('/api/setup/reviewed')\">I've reviewed my sources</button>"
        )
    last = repo.get_setting(conn, "briefings.last") or {}
    brief_body = (
        f"<b>{e(last.get('title', ''))}</b> <span class='muted small'>{e(last.get('at', '')[:16].replace('T', ' '))}</span>"
        + "".join(f"<div>{e(line)}</div>" for line in last.get("lines", []))
        if last and not phone
        else "<span class='muted small'>No briefing yet today.</span>"
    )
    briefing_card = (
        ""
        if phone
        else card(
            f"<div id='brief'>{brief_body}</div><div class='row'><span class='grow'></span>"
            "<button onclick=\"api('/api/briefing').then(r=>{document.getElementById('brief').innerHTML="
            "'<b>'+esc(r.title)+'</b>'+r.lines.map(l=>'<div>'+esc(l)+'</div>').join('')+r.notes.map(n=>'<div class='muted small'>'+esc(n)+'</div>').join('')})\">"
            "Brief me now</button></div>"
        )
    )
    body = (
        welcome
        + controls
        + briefing_card
        + _voice_card(state)
        + ask_box
        + f"<h2>Today</h2><p class='muted small'>{len(sessions)} sessions, {dur(total)} active{extra}</p>"
    )
    body += "".join(_session_card(conn, s, summaries.get(s.id)) for s in reversed(sessions))
    if not sessions:
        body += card("<span class='muted'>No work sessions yet today.</span>")
    return page("Today", body, path="/", status=_status(state, conn), phone=phone)


def timeline(
    state: WebState, conn: sqlite3.Connection, day: str | None, *, phone: bool = False
) -> str:
    now = state.clock.now()
    try:
        when_day = datetime.fromisoformat(day).astimezone() if day else now
    except ValueError:
        when_day = now
    start, end = day_bounds(when_day)
    prev_d, next_d = start - timedelta(days=1), start + timedelta(days=1)
    nav = (
        f"<div class='row'><a class='btn' href='/timeline?day={prev_d:%Y-%m-%d}'>← {prev_d:%a %d}</a>"
        f"<b class='grow' style='text-align:center'>{start:%A %d %B}</b>"
        f"<a class='btn' href='/timeline?day={next_d:%Y-%m-%d}'>{next_d:%a %d} →</a></div>"
    )
    day_roll = rollups.load(conn, f"day:{start:%Y-%m-%d}")
    roll = ""
    if day_roll:
        b = day_roll.body
        items = "".join(f"<li>{e(i['text'])}</li>" for i in b.get("by_project", []))
        roll = card(f"<b>{e(b.get('headline', ''))}</b><ul>{items}</ul>")
    sessions = load_sessions(conn, start, end)
    summaries = _summaries(conn, start, end)
    cards = "".join(_session_card(conn, s, summaries.get(s.id)) for s in sessions)
    body = nav + roll + (cards or card("<span class='muted'>No sessions that day.</span>"))
    return page("Timeline", body, path="/timeline", status=_status(state, conn), phone=phone)


def projects(state: WebState, conn: sqlite3.Connection, *, phone: bool = False) -> str:
    now = state.clock.now()
    week = {
        pid: ms
        for pid, ms in conn.execute(
            "SELECT project_id, sum(active_ms) FROM sessions WHERE ts_start >= ? GROUP BY 1",
            (to_ms(now - timedelta(days=7)),),
        )
    }
    top = max(week.values(), default=1) or 1
    rows = []
    for p in sorted(load_projects(conn), key=lambda p: -(week.get(p.id) or 0)):
        if p.id == NONE_PROJECT:
            continue
        ms = week.get(p.id) or 0
        has_state = rollups.load(conn, f"project:{p.id}") is not None
        bar = (
            f"<span class='bar' style='width:{max(2, int(160 * ms / top))}px'></span>" if ms else ""
        )
        rows.append(
            f"<tr><td><a href='/projects?id={e(p.id)}'>{e(p.id)}</a>"
            f"{' <span class=pill>state</span>' if has_state else ''}</td>"
            f"<td>{bar} <span class='small muted'>{dur(ms)}</span></td>"
            f"<td class='small muted'>{e(p.roots[0] if p.roots else '')}</td></tr>"
        )
    body = card(
        "<table><tr><th>Project</th><th>Last 7 days</th><th>Folder</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return page("Projects", body, path="/projects", status=_status(state, conn), phone=phone)


def project(state: WebState, conn: sqlite3.Connection, pid: str, *, phone: bool = False) -> str:
    now = state.clock.now()
    note = rollups.load(conn, f"project:{pid}")
    if note:
        b = note.body

        def section(key: str, label: str) -> str:
            items = b.get(key) or []
            if not items:
                return ""
            lis = "".join(f"<li>{e(i['text'] if isinstance(i, dict) else i)}</li>" for i in items)
            return f"<div><b class='small'>{label}</b><ul>{lis}</ul></div>"

        state_html = card(
            f"<div><b>{e(b.get('goal', ''))}</b></div>"
            + section("in_progress", "In progress")
            + section("blockers", "Blockers")
            + section("next_steps", "Next steps")
            + section("done", "Done recently")
            + (
                f"<div class='small'>Key files: {pills(b.get('key_files', []))}</div>"
                if b.get("key_files")
                else ""
            )
            + f"<div class='small muted'>Updated {when(note.created)}</div>"
        )
    else:
        state_html = card(
            "<span class='muted'>No current-state note yet: it's written in the background once sessions are summarised.</span>"
        )
    start = now - timedelta(days=30)
    sessions = [s for s in load_sessions(conn, start, now) if s.project_id == pid]
    summaries = _summaries(conn, start, now)
    body = "<h2>Current state</h2>" + state_html + "<h2>Recent sessions</h2>"
    body += "".join(_session_card(conn, s, summaries.get(s.id)) for s in reversed(sessions[-15:]))
    if not sessions:
        body += card("<span class='muted'>No sessions in the last 30 days.</span>")
    if not phone:
        body += f"""<h2>Data</h2>{
            card(f'''<div class="row">
<a class="btn" href="/export/markdown?project={e(pid)}">Export as Markdown</a>
<a class="btn" href="/export/json?project={e(pid)}">Export as JSON</a><span class="grow"></span>
<button class="danger" onclick="const c=prompt('Delete everything captured for this project? Type its id to confirm: {e(pid)}');
if(c) act('/api/projects/delete',{{project:'{e(pid)}',confirm:c}})">Delete this project's data</button></div>''')
        }"""
    return page(pid, body, path="/projects", status=_status(state, conn), phone=phone)


def ask_page(q: str, *, phone: bool = False) -> str:
    body = f"""{
        card(f'''<form class="row" onsubmit="go(event)">
<input type="text" id="q" class="grow" value="{e(q)}" placeholder="where did I leave off on …? what did I do yesterday?">
<label class="small"><input type="checkbox" id="fu"> follow-up</label>
<button class="primary">Ask</button></form>''')
    }
<div id="out"></div>
<script>
async function go(ev) {{ ev && ev.preventDefault(); const q = document.getElementById('q').value.trim();
  if (!q) return; const out = document.getElementById('out');
  out.innerHTML = '<div class="card muted">Thinking… (the first answer after a while can take 15 s)</div>';
  try {{ const r = await api('/api/ask', {{question:q, follow_up:document.getElementById('fu').checked}});
    if (r.remember) {{ out.innerHTML = '<div class="card">' + esc(r.text) +
      ' <button onclick="act(\\'/api/memory/add\\',{{text:' + JSON.stringify(r.remember).replace(/"/g,'&quot;') +
      '}},false).then(()=>{{document.getElementById(\\'out\\').innerHTML=\\'<div class=card>Remembered.</div>\\'}})">Remember</button></div>'; return; }}
    let h = '<div class="card"><div>' + r.sentences.map(s => (s.text.startsWith('\\n') ? '<br>' : ' ') +
      esc(s.text.trim()) + s.refs.map(n => ' <a href="#ev' + n + '">[' + n + ']</a>').join('')).join('') + '</div>';
    if (r.answered) h += '<div class="small muted">Confidence: ' + esc(r.confidence) + ' · ' +
      esc(r.intent) + (r.project ? ' · ' + esc(r.project) : '') + (r.range ? ' · ' + esc(r.range) : '') + '</div>';
    if (r.facts.length) h += '<div class="small muted">' + r.facts.map(esc).join(' ') + '</div>';
    h += '</div>';
    if (r.evidence.length) h += '<h2>Evidence</h2>' + r.evidence.map(x => '<div class="card small" id="ev' +
      x.ref + '"><b>[' + x.ref + ']</b> ' + esc(x.when) + ' <span class="pill">' + esc(x.kind) +
      '</span> ' + esc(x.text) + (x.detail ? '<div class="muted">' + esc(x.detail) + '</div>' : '') +
      '</div>').join('');
    out.innerHTML = h; document.getElementById('fu').checked = true;
  }} catch (e) {{ out.innerHTML = ''; }} }}
if (document.getElementById('q').value) go();
</script>"""
    return page("Ask", body, path="/ask", phone=phone)


def answer_json(conn: sqlite3.Connection, a: Answer) -> dict[str, Any]:
    cited = a.cited() if a.mode == "llm" else a.gathered.pack.lines[:12]
    q = a.question
    return {
        "answered": a.answered,
        "mode": a.mode,
        "sentences": [{"text": s, "refs": r} for s, r in a.sentences],
        "confidence": a.confidence,
        "intent": q.intent,
        "project": q.project.id if q.project else None,
        "range": q.range_label,
        "facts": a.gathered.facts if a.mode == "llm" else [],
        "evidence": [
            {
                "ref": x.ref,
                "when": when(x.ts, "%a %d %b %H:%M"),
                "kind": x.kind,
                "text": x.text,
                "detail": x.detail,
            }
            for x in cited
        ],
        "notes": a.notes,
    }


def inbox_page(state: WebState, conn: sqlite3.Connection) -> str:
    from personal_os.projects.inbox import inbox

    groups = inbox(conn, state.clock, days=7)
    options = "".join(f"<option value='{e(p.id)}'>{e(p.id)}</option>" for p in load_projects(conn))
    cards = []
    for i, g in enumerate(groups):
        payload = json.dumps({"kind": g.kind, "key": g.key, "event_ids": g.event_ids})
        guess = ", ".join(k for k, _ in g.guesses.most_common(2) if k != "-")
        samples = "".join(f"<div class='evidence'>{e(s)}</div>" for s in g.samples)
        cards.append(
            card(
                f"<div class='row'><b>{e(g.label)}</b><span class='muted small'>{g.count} items, "
                f"{dur(int(g.seconds * 1000))}{' · guess: ' + e(guess) if guess else ''}</span>"
                f"<span class='grow'></span><select id='p{i}'>{options}</select>"
                f"<button class='primary' onclick='act(\"/api/inbox/assign\", Object.assign({e(payload)}, "
                f'{{project: document.getElementById("p{i}").value}}))\'>Assign</button></div>{samples}'
            )
        )
    body = "".join(cards) or card(
        "<span class='muted'>Inbox is empty: everything has a confident project.</span>"
    )
    intro = "<p class='muted small'>Pick a project (or <b>none</b> for not-work). The choice is learned for the whole group.</p>"
    return page("Inbox", intro + body, path="/inbox", status=_status(state, conn))


def actions_page(state: WebState, conn: sqlite3.Connection) -> str:
    killed = killswitch.engaged(state.config.runtime_dir)
    kill = (
        "<span class='bad'>Kill switch is ON: nothing runs.</span><span class='grow'></span>"
        "<button class='primary' onclick=\"act('/api/actions/resume')\">Resume actions</button>"
        if killed
        else "<span class='ok'>Actions are on.</span><span class='grow'></span>"
        "<button class='danger' onclick=\"act('/api/actions/kill')\">Kill switch (Ctrl+Alt+Shift+K)</button>"
    )
    do_box = card(
        """<form class="row" onsubmit="doit(event)"><input type="text" id="req" class="grow"
placeholder="open my personal-os folder in vscode · remind me at 5pm to stretch · take a screenshot">
<button class="primary">Do it</button></form><div id="steps"></div>
<script>
async function doit(ev) { ev.preventDefault(); const box = document.getElementById('steps');
  box.innerHTML = '<p class="muted">Planning…</p>';
  try { const r = await api('/api/do', {request: document.getElementById('req').value});
    let h = r.reply ? '<p>' + esc(r.reply) + '</p>' : '';
    if (r.error) h += '<p class="bad">' + esc(r.error) + '</p>';
    for (const s of r.steps) {
      h += '<div class="card"><b>' + esc(s.tool) + '</b> <span class="pill">' + esc(s.status) + '</span> ' +
        esc(s.message) + (s.preview ? '<pre>' + esc(s.preview) + '</pre>' : '');
      if (s.pending) h += (s.tainted ? '<p class="warn small">Follows captured content: check it.</p>' : '') +
        '<button class="primary" onclick="act(\\'/api/actions/approve\\',{pending:\\'' + s.pending +
        '\\'})">Approve</button> <button onclick="act(\\'/api/actions/decline\\',{pending:\\'' +
        s.pending + '\\'})">Decline</button>';
      h += '</div>'; }
    box.innerHTML = h || '<p class="muted">Nothing to do.</p>';
  } catch (e) { box.innerHTML = ''; } }
</script>"""
    )
    now = state.clock.now()
    unlocked = killswitch.unlocks(state.config.runtime_dir, now)
    t3 = [t for t in build_registry().all() if int(t.tier) == 3]
    unlock_rows = "".join(
        f"<div class='row'><b>{e(t.name)}</b><span class='muted small'>{e(t.summary)}</span>"
        f"<span class='grow'></span>"
        + (
            f"<span class='warn small'>unlocked until {when(unlocked[t.name], '%H:%M')}</span>"
            if t.name in unlocked
            else f"<button onclick=\"if(confirm('Allow {e(t.name)} for 10 minutes? Each use still asks.')) "
            f"act('/api/actions/unlock',{{tool:'{e(t.name)}',minutes:10}})\">Unlock 10 min</button>"
        )
        + "</div>"
        for t in t3
    )
    log_rows = []
    for stages in latest(conn, 30):
        first, final = stages[0], stages[-1]
        flow = " › ".join(s.stage for s in stages)
        undo = ""
        if any(s.undo for s in stages) and not any(s.stage == "undone" for s in stages):
            undo = f"<button class='small' onclick=\"act('/api/actions/undo',{{action:'{first.action_id[-8:]}'}})\">Undo</button>"
        log_rows.append(
            f"<tr><td class='small'>{when(first.ts, '%d %b %H:%M:%S')}</td><td><b>{e(first.tool)}</b> "
            f"<span class='muted small'>tier {first.tier}, {e(first.origin)}{', after captured content' if first.tainted else ''}</span>"
            f"<div class='small muted'>{e(flow)}</div><div class='small'>{e(final.detail[:160])}</div>"
            f"{'<div class=small>request: ' + e(first.request[:120]) + '</div>' if first.request else ''}</td>"
            f"<td>{undo}</td></tr>"
        )
    body = (
        card(f"<div class='row'>{kill}</div>")
        + "<h2>Ask it to do something</h2>"
        + do_box
        + "<h2>Blocked tools</h2>"
        + card(
            unlock_rows + "<div class='row'><span class='grow'></span>"
            "<button onclick=\"act('/api/actions/lock')\">Block all again</button></div>"
        )
        + "<h2>Log</h2>"
        + card(
            "<table>" + "".join(log_rows) + "</table>"
            if log_rows
            else "<span class='muted'>No actions yet.</span>"
        )
    )
    return page("Actions", body, path="/actions", status=_status(state, conn))


def memory_page(state: WebState, conn: sqlite3.Connection) -> str:
    facts = memory.all_facts(conn)
    rows = "".join(
        f"<div class='row card'><input type='text' class='grow' id='f{i}' value='{e(f.text)}'>"
        f"<span class='muted small'>{when(f.created, '%d %b')}</span>"
        f"<button onclick=\"act('/api/memory/update',{{id:'{f.id}',text:document.getElementById('f{i}').value}})\">Save</button>"
        f"<button class='danger' onclick=\"act('/api/memory/delete',{{id:'{f.id}'}})\">Forget</button></div>"
        for i, f in enumerate(facts)
    )
    add = card(
        "<form class='row' onsubmit=\"event.preventDefault(); act('/api/memory/add',"
        "{text:document.getElementById('nf').value})\"><input type='text' id='nf' class='grow' "
        "placeholder='e.g. the staging database runs on port 5433'><button class='primary'>Remember</button></form>"
    )
    intro = (
        "<p class='muted small'>Facts you've asked me to remember. I use them when answering, "
        "and nothing is added here unless you say so (type “remember that …” in Ask).</p>"
    )
    return page(
        "Memory",
        intro + add + (rows or card("<span class='muted'>Nothing yet.</span>")),
        path="/memory",
        status=_status(state, conn),
    )


SOURCES = [
    (
        "window",
        "Active window",
        "Which app and window title is in front, so time can be put against projects. Never what's on the screen or what you type.",
    ),
    ("idle", "Idle, lock and sleep", "When you step away, so breaks don't count as work."),
    (
        "git",
        "Git repositories",
        "Branch, commits and changed files in repos under your watched folders. Never file contents.",
    ),
    (
        "terminal",
        "Terminal commands",
        "Commands you run in PowerShell or Git Bash and whether they failed, through a hook you install. A command starting with a space is never recorded.",
    ),
    (
        "fs",
        "Files",
        "Which files under your watched folders change, with line counts. Never a copy of the file.",
    ),
    (
        "browser",
        "Browser",
        "Pages you actually read (title, readable text, time), through the extension. Never incognito, blocked sites, password pages or form contents.",
    ),
]


def privacy_page(state: WebState, conn: sqlite3.Connection) -> str:
    cfg = state.config
    observers = cfg.observers
    src_rows = []
    for name, label, text in SOURCES:
        on = getattr(observers, name).enabled
        src_rows.append(
            f"<div class='row card'><div class='grow'><b>{label}</b> "
            f"<span class='{'ok' if on else 'muted'} small'>{'on' if on else 'off'}</span>"
            f"<div class='small muted'>{e(text)}</div></div>"
            f"<button onclick=\"act('/api/settings',{{key:'observers.{name}.enabled',value:{'false' if on else 'true'}}})\">"
            f"{'Turn off' if on else 'Turn on'}</button></div>"
        )
    restart = card(
        "<div class='row'><span class='muted small grow'>Turning a source on or off applies when capture restarts.</span>"
        "<button onclick=\"act('/api/daemon/restart',{},false)\">Restart capture</button></div>"
    )
    paused = pause_mod.is_paused(conn, state.clock)
    control = card(
        "<div class='row'>"
        + (
            "<button class='primary' onclick=\"act('/api/resume')\">Resume capture</button>"
            if paused
            else "<button onclick=\"act('/api/pause')\">Pause capture</button>"
        )
        + "<span class='grow'></span><span class='small'>Forget the last</span>"
        + "".join(
            f"<button onclick=\"if(confirm('Delete everything captured in the last {m} minutes?')) act('/api/forget',{{minutes:{m}}})\">{m} min</button>"
            for m in (15, 60, 240)
        )
        + "</div>"
    )
    r = cfg.retention
    retention = card(
        "<table>"
        + "".join(
            f"<tr><td>{label}</td><td><input type='number' min='1' id='r_{key}' value='{getattr(r, key)}' style='width:80px'> days "
            f"<button onclick=\"act('/api/settings',{{key:'retention.{key}',value:+document.getElementById('r_{key}').value}})\">Save</button></td></tr>"
            for key, label in (
                ("raw_days", "Raw events"),
                ("page_text_days", "Page text"),
                ("terminal_output_days", "Terminal output"),
                ("sensitive_days", "Sensitive events"),
            )
        )
        + "</table><p class='small muted'>Summaries and rollups are kept until you forget or delete them.</p>"
    )

    def list_editor(key: str, label: str, values: list[str]) -> str:
        chips = "".join(
            f"<span class='pill'>{e(v)} <a href='#' onclick=\"event.preventDefault(); "
            f"act('/api/settings',{{key:'{key}',value:{e(json.dumps([x for x in values if x != v]))}}})\">×</a></span>"
            for v in values
        )
        return (
            f"<div><b class='small'>{label}</b><div>{chips}</div><form class='row' onsubmit=\"event.preventDefault(); "
            f"act('/api/settings',{{key:'{key}',value:{e(json.dumps(values))}.concat([document.getElementById('a_{key}').value.trim()])}})\">"
            f"<input type='text' id='a_{key}' placeholder='add…' style='max-width:260px'><button>Add</button></form></div>"
        )

    lists = card(
        list_editor(
            "privacy.blocked_domains", "Never captured (sites)", cfg.privacy.blocked_domains
        )
        + list_editor("privacy.blocked_apps", "Never captured (apps)", cfg.privacy.blocked_apps)
        + list_editor(
            "privacy.sensitive_domains", "Site and time only", cfg.privacy.sensitive_domains
        )
    )
    info = maintenance.inspect(conn, cfg.db_path)
    stored = card(
        f"<p class='small muted'>Database {info.db_bytes / 1e6:.1f} MB.</p><table><tr><th>Source</th><th>Kind</th><th>Rows</th><th>Text</th></tr>"
        + "".join(
            f"<tr><td>{e(s)}</td><td>{e(c)}</td><td>{n}</td><td>{b / 1e3:.0f} kB</td></tr>"
            for s, c, n, b in info.by_source
        )
        + "</table><div class='row'><a class='btn' href='/export/markdown'>Export everything (Markdown)</a>"
        "<a class='btn' href='/export/json'>Export everything (JSON)</a></div>"
    )
    lan = cfg.api.lan
    addresses = state.lan_addresses()
    devs = auth.devices(conn)
    dev_rows = "".join(
        f"<div class='row'><b>{e(d.name)}</b><span class='muted small'>paired {when(d.created, '%d %b')}, last seen {when(d.last_seen)}</span>"
        f"<span class='grow'></span>"
        + (
            "<span class='muted small'>revoked</span>"
            if d.revoked
            else f"<button class='danger' onclick=\"act('/api/lan/revoke',{{device:'{d.id}'}})\">Revoke</button>"
        )
        + "</div>"
        for d in devs
    )
    url = f"https://{addresses[0]}:{lan.port}" if addresses else "(no network address found)"
    firewall = (
        f'New-NetFirewallRule -DisplayName "Personal OS phone access" -Direction Inbound '
        f"-Protocol TCP -LocalPort {lan.port} -Profile Private -Action Allow"
    )
    phone = card(
        f"<div class='row'><b>Phone access</b><span class='{'ok' if lan.enabled else 'muted'} small'>{'on' if lan.enabled else 'off'}</span>"
        f"<span class='grow'></span><button onclick=\"act('/api/settings',{{key:'api.lan.enabled',value:{'false' if lan.enabled else 'true'}}})\">"
        f"{'Turn off' if lan.enabled else 'Turn on'}</button></div>"
        "<p class='small muted'>Lets a phone on your Wi-Fi see Today, Timeline, Projects and Ask, and pause or forget. "
        "Never actions, settings or exports. Applies after a capture restart.</p>"
        + (
            f"<p>On the phone, open <b>{e(url)}</b> (accept the certificate warning once: it's this laptop's own certificate), then enter the code.</p>"
            "<button onclick=\"api('/api/lan/pair').then(r=>{document.getElementById('code').textContent=r.code})\">Show pairing code</button> "
            "<b id='code' style='font-size:22px;letter-spacing:3px'></b>"
            f"<details><summary class='small'>Windows Firewall (one time, as administrator)</summary><pre>{e(firewall)}</pre></details>"
            if lan.enabled
            else ""
        )
        + (f"<h2>Paired phones</h2>{dev_rows}" if devs else "")
    )
    body = (
        "<h2>Capture</h2>"
        + control
        + "<h2>Sources</h2>"
        + "".join(src_rows)
        + restart
        + "<h2>Retention</h2>"
        + retention
        + "<h2>Blocklists</h2>"
        + lists
        + "<h2>What's stored</h2>"
        + stored
        + "<h2>Voice, briefings and Google</h2>"
        + _assistant_section(state)
        + "<h2>Phone</h2>"
        + phone
    )
    info_daemon = runtime.daemon_info(cfg.runtime_dir)
    if not info_daemon.running:
        body = card("<span class='warn'>Capture isn't running.</span>") + body
    return page("Privacy", body, path="/privacy", status=_status(state, conn))


def _assistant_section(state: WebState) -> str:
    """Voice, wake word, voice output, daily briefings and Google: what each does, on/off."""

    from personal_os.integrations import google
    from personal_os.voice import wake

    cfg = state.config
    v, br = cfg.voice, cfg.briefings

    def toggle(key: str, on: bool, label: str, text: str, extra: str = "") -> str:
        return (
            f"<div class='row card'><div class='grow'><b>{label}</b> "
            f"<span class='{'ok' if on else 'muted'} small'>{'on' if on else 'off'}</span>"
            f"<div class='small muted'>{text}</div>{extra}</div>"
            f"<button onclick=\"act('/api/settings',{{key:'{key}',value:{'false' if on else 'true'}}})\">"
            f"{'Turn off' if on else 'Turn on'}</button></div>"
        )

    from personal_os.voice.main import status as voice_status

    running = voice_status(cfg.runtime_dir) if v.enabled else None
    voice_extra = (
        f"<div class='small'>voice process: {e(running['state'])}</div>" if running else ""
    )
    has_model = wake.available(cfg.data_dir)
    wake_text = (
        "Listens for “Donna” (also “hey Donna”, “hello Donna”) with Vosk, on this laptop; "
        "nothing is stored, and say “stop talking” to stop a reply."
        + (
            ""
            if has_model
            else " <b>Model not downloaded yet</b>: run <code>personal-os voice setup</code>."
        )
    )
    voices = "".join(
        f"<button {'class=primary' if v.tts == name else ''} onclick=\"act('/api/settings',{{key:'voice.tts',value:'{name}'}})\">{label}</button> "
        for name, label in (
            ("windows", "Windows voice (offline)"),
            ("piper", "Piper (local)"),
            ("speechify", "Speechify (cloud)"),
        )
    )
    connected = google.connected(cfg.data_dir)
    return (
        toggle(
            "voice.enabled",
            v.enabled,
            "Voice",
            "Press Ctrl+Alt+Shift+V and speak. Speech is recognised on this laptop (faster-whisper); only the command text is kept, in the action log.",
            voice_extra,
        )
        + toggle("voice.wake_word_enabled", v.wake_word_enabled, "Wake word", wake_text)
        + card(
            "<b>Voice for replies</b><div class='small muted'>Speechify sends the sentence being spoken to Speechify (needs your key: <code>personal-os voice speechify-key</code>); the others stay on this laptop.</div><div class='row'>"
            + voices
            + "</div>"
        )
        + toggle(
            "briefings.enabled",
            br.enabled,
            "Daily briefings",
            f"A morning briefing the first time you're active after {e(br.morning_after)}, and an end-of-day summary after {e(br.evening_after)}. Built from your summaries, commits, reminders and (if connected) Google.",
        )
        + card(
            f"<b>Google Calendar and Gmail</b> <span class='{'ok' if connected else 'muted'} small'>{'connected' if connected else 'not connected'}</span>"
            "<div class='small muted'>Read-only: calendar events, and the sender and subject of unread mail (never bodies). Fetched when needed, not stored. "
            + (
                "Disconnect with <code>personal-os google disconnect</code>."
                if connected
                else "Set up with docs/GOOGLE.md, then <code>personal-os google connect CLIENT_SECRET.json</code>."
            )
            + "</div>"
        )
    )


def _voice_card(state: WebState) -> str:
    """Voice at a glance: is it running, and the last few things it heard and did."""
    from personal_os.voice.main import history, local_time
    from personal_os.voice.main import status as voice_status

    if not state.config.voice.enabled:
        return ""
    vs = voice_status(state.config.runtime_dir)
    cls = "ok" if vs.get("running") else "bad"
    rows = "".join(
        f"<div class='small'><b>{e(local_time(x.get('at', ''), '%H:%M'))}</b> {e(x.get('heard') or '(nothing heard)')} "
        f"<span class='pill'>{e(x.get('route', '?'))}</span> <span class='muted'>{e(x.get('total_s', '?'))} s</span>"
        + (f"<div class='muted'>{e((x.get('reply') or '')[:160])}</div>" if x.get("reply") else "")
        + (f"<div class='bad'>{e(x['error'])}</div>" if x.get("error") else "")
        + "</div>"
        for x in reversed(history(state.config.runtime_dir, 5))
    )
    hint = (
        ""
        if vs.get("running")
        else " The voice process isn't running; restart capture (tray → Restart capture)."
    )
    return card(
        f"<b>Voice</b> <span class='{cls} small'>{e(vs['state'])}</span>"
        f"<span class='muted small'> · Ctrl+Alt+Shift+V, then speak.{e(hint)}</span>"
        + (rows or "<div class='muted small'>Nothing heard yet.</div>")
    )
