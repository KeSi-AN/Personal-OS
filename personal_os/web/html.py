"""Small pieces shared by the dashboard's pages (web/v2.py has the layout): escaping, times,
the fetch helper. No build step, nothing from the web."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from html import escape

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


def e(value: object) -> str:
    return escape("" if value is None else str(value))


def when(dt: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return dt.astimezone().strftime(fmt) if dt else "-"


def dur(ms: int) -> str:
    m = int(ms) // 60000
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def page(title: str, body: str, *, path: str, status: str = "", phone: bool = False) -> str:
    """A page in the dashboard's layout (web/v2.py): under Settings, or with no navigation for
    signing in (path ""). The old dashboard's own layout and pages went on 21 Nov."""
    from personal_os.web import v2

    if not path:
        return v2.bare(title, body)
    return v2.classic(title, body, status=status, phone=phone)


def pills(items: Iterable[str]) -> str:
    return "".join(f'<span class="pill">{e(i)}</span>' for i in items)


def card(body: str, extra: str = "") -> str:
    return f'<div class="card" {extra}>{body}</div>'
