"""Server-rendered pages: one layout, a few components, no build step, nothing from the web."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from html import escape

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0;
  --accent:#2f5bd3; --ok:#1f8a4c; --warn:#b26b00; --bad:#c0392b; --chip:#eef1fb; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --card:#1d1d20; --ink:#ececee;
  --muted:#9a9aa2; --line:#2c2c31; --accent:#7f9cff; --ok:#4cc483; --warn:#f0a63a;
  --bad:#ff6b5e; --chip:#262a38; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; z-index:2; }
a { color:var(--accent); }
header b { margin-right:8px; }
nav a { color:var(--muted); text-decoration:none; margin-right:12px; }
nav a.on, nav a:hover { color:var(--ink); }
main { max-width:980px; margin:0 auto; padding:18px 16px 60px; }
h1 { font-size:22px; margin:6px 0 14px; } h2 { font-size:17px; margin:22px 0 8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin:10px 0; }
.muted { color:var(--muted); } .small { font-size:13px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.grow { flex:1; }
.pill { display:inline-block; padding:1px 9px; border-radius:99px; background:var(--chip);
  font-size:12px; margin:0 4px 4px 0; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
button, .btn { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--ink);
  border-radius:8px; padding:6px 12px; cursor:pointer; text-decoration:none; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.danger { color:var(--bad); }
input[type=text], input[type=number], textarea, select { font:inherit; color:var(--ink);
  background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:7px 10px; }
input[type=text] { width:100%; }
details summary { cursor:pointer; color:var(--muted); }
table { width:100%; border-collapse:collapse; } td, th { text-align:left; padding:6px 8px;
  border-bottom:1px solid var(--line); vertical-align:top; } th { color:var(--muted); font-weight:500; }
.evidence { font-size:13px; color:var(--muted); margin:2px 0 0; }
.bar { height:8px; border-radius:4px; background:var(--accent); display:inline-block; }
pre { white-space:pre-wrap; font-size:13px; background:var(--bg); padding:8px; border-radius:8px; }
#toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); background:var(--ink);
  color:var(--bg); padding:8px 14px; border-radius:8px; display:none; }
@media (max-width:600px) { main { padding:12px; } nav a { margin-right:8px; } }
"""

JS = """
async function api(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json',
    'X-POS':'1'}, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.detail || ('error ' + r.status)); throw new Error(data.detail); }
  return data;
}
function toast(msg) { const t = document.getElementById('toast'); t.textContent = msg;
  t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 3500); }
function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }
async function act(path, body, reload) { const r = await api(path, body);
  if (r.message) toast(r.message); if (reload !== false) setTimeout(() => location.reload(), 400); return r; }
"""

NAV = [
    ("/", "Today"), ("/timeline", "Timeline"), ("/projects", "Projects"), ("/ask", "Ask"),
    ("/inbox", "Inbox"), ("/actions", "Actions"), ("/memory", "Memory"), ("/privacy", "Privacy"),
]  # fmt: skip
PHONE_NAV = {"/", "/timeline", "/projects", "/ask"}


def e(value: object) -> str:
    return escape("" if value is None else str(value))


def when(dt: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return dt.astimezone().strftime(fmt) if dt else "-"


def dur(ms: int) -> str:
    m = int(ms) // 60000
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def page(title: str, body: str, *, path: str, status: str = "", phone: bool = False) -> str:
    links = "".join(
        f'<a href="{href}" class="{"on" if href == path else ""}">{e(label)}</a>'
        for href, label in NAV
        if not phone or href in PHONE_NAV
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Personal OS</title><style>{CSS}</style><script>{JS}</script></head><body>
<header><b>Personal OS</b><nav>{links}</nav><span class="grow"></span>{status}</header>
<main><h1>{e(title)}</h1>{body}</main><div id="toast"></div></body></html>"""


def pills(items: Iterable[str]) -> str:
    return "".join(f'<span class="pill">{e(i)}</span>' for i in items)


def card(body: str, extra: str = "") -> str:
    return f'<div class="card" {extra}>{body}</div>'
