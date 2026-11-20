"""The dashboard's pages. Each reads through the same functions the CLI uses."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from personal_os.actions import killswitch
from personal_os.actions.audit import latest
from personal_os.actions.tools import build_registry
from personal_os.context import memory
from personal_os.core import pause as pause_mod
from personal_os.core import runtime
from personal_os.projects.registry import load_projects
from personal_os.storage import maintenance
from personal_os.web import auth
from personal_os.web.html import card, dur, e, page, when

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


# --- pages -------------------------------------------------------------------------------------


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
