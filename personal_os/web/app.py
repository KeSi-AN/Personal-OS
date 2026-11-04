"""The dashboard and its JSON API (DESIGN.md §6.4).

Two apps share this code: the laptop's (127.0.0.1, everything) and the phone's (LAN, HTTPS,
read-only pages plus ask, pause/resume and forget). Every request is checked for the right Host
(DNS rebinding), a session or paired-device cookie, and, for anything that changes state, the
X-POS header and a same-origin Origin (other websites can't send either).
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from personal_os.actions import killswitch
from personal_os.actions.base import Origin
from personal_os.actions.desktop import Desktop
from personal_os.actions.executor import Deferred, Executor
from personal_os.actions.tools import build_registry
from personal_os.config.loader import set_value
from personal_os.config.schema import Config
from personal_os.context import memory
from personal_os.context.question import ParsedQuestion
from personal_os.core import pause as pause_mod
from personal_os.core.clock import Clock
from personal_os.core.wiring import build_pipeline
from personal_os.llm.provider import LLMProvider
from personal_os.storage import maintenance
from personal_os.storage.db import connect
from personal_os.web import auth, pages

SESSION_COOKIE = "pos_session"
DEVICE_COOKIE = "pos_device"
PHONE_ROUTES = {
    ("GET", "/"),
    ("GET", "/timeline"),
    ("GET", "/projects"),
    ("GET", "/ask"),
    ("POST", "/api/ask"),
    ("POST", "/api/pause"),
    ("POST", "/api/resume"),
    ("POST", "/api/forget"),
    ("GET", "/pair"),
    ("POST", "/pair"),
}
OPEN_ROUTES = {"/login", "/pair", "/favicon.ico"}
PENDING_TTL = timedelta(minutes=10)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    follow_up: bool = False


class PauseBody(BaseModel):
    minutes: int | None = Field(None, ge=1, le=24 * 60)


class ForgetBody(BaseModel):
    minutes: int = Field(ge=1, le=7 * 24 * 60)


class AssignBody(BaseModel):
    kind: str
    key: str
    event_ids: list[str]
    project: str


class DoBody(BaseModel):
    request: str = Field(min_length=1, max_length=500)


class PendingBody(BaseModel):
    pending: str


class UndoBody(BaseModel):
    action: str


class UnlockBody(BaseModel):
    tool: str
    minutes: int = Field(10, ge=1, le=60)


class FactBody(BaseModel):
    text: str = Field("", max_length=memory.MAX_FACT)
    id: str | None = None


class SettingBody(BaseModel):
    key: str
    value: Any


class DeleteProjectBody(BaseModel):
    project: str
    confirm: str


class DeviceBody(BaseModel):
    device: str


@dataclass
class PendingAction:
    tool: str
    args: dict[str, Any]
    tainted: bool
    request: str
    preview: str
    tier: int
    created: Any


@dataclass
class WebState:
    config: Config
    clock: Clock
    home: Path | None = None
    provider: Callable[[], LLMProvider | None] = lambda: None
    desktop: Callable[[], Desktop] | None = None
    restart: Callable[[], None] | None = None
    lan_addresses: Callable[[], list[str]] = lambda: []
    sessions: auth.Sessions = field(init=False)
    pairing: auth.Pairing = field(init=False)
    pending: dict[str, PendingAction] = field(default_factory=dict)
    followups: dict[str, ParsedQuestion] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sessions = auth.Sessions(self.clock)
        self.pairing = auth.Pairing(self.clock)

    @contextlib.contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.config.db_path)
        try:
            yield conn
        finally:
            conn.close()


def create_app(state: WebState, *, lan: bool = False) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    port = state.config.api.lan.port if lan else state.config.api.port

    def allowed_hosts() -> set[str]:
        if lan:
            return {f"{a}:{port}" for a in state.lan_addresses()}
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        host = request.headers.get("host", "")
        if host not in allowed_hosts():
            return JSONResponse({"detail": "wrong host"}, status_code=421)
        path, method = request.url.path, request.method
        if lan and (method, path) not in PHONE_ROUTES:
            return JSONResponse({"detail": "not available from a phone"}, status_code=403)
        if method != "GET":
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{host}"
            if request.headers.get("x-pos") != "1" and path != "/pair":
                return JSONResponse({"detail": "missing X-POS header"}, status_code=403)
            if origin is not None and origin != expected:
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if path not in OPEN_ROUTES:
            if lan:
                with state.db() as conn:
                    ok = auth.device_for(conn, state.clock, request.cookies.get(DEVICE_COOKIE))
                if not ok:
                    return RedirectResponse("/pair", status_code=303)
            elif not state.sessions.valid(request.cookies.get(SESSION_COOKIE)):
                return HTMLResponse(pages.login_needed(), status_code=401)
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    def viewer(request: Request) -> str:
        return request.cookies.get(DEVICE_COOKIE if lan else SESSION_COOKIE, "")

    # --- login and pairing -----------------------------------------------------------------

    @app.get("/login")
    def login(code: str = "") -> Response:
        with state.db() as conn:
            ok = not lan and auth.use_login_code(conn, state.clock, code)
        if not ok:
            return HTMLResponse(pages.login_needed(expired=bool(code)), status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE,
            state.sessions.create(),
            httponly=True,
            samesite="strict",
            max_age=int(auth.SESSION_TTL.total_seconds()),
        )
        return resp

    @app.get("/pair", response_class=HTMLResponse)
    def pair_form() -> str:
        return pages.pair_form()

    @app.post("/pair")
    async def pair(request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        code = (form.get("code") or [""])[0]
        name = (form.get("name") or ["phone"])[0]
        if not lan or not state.pairing.redeem(code):
            return HTMLResponse(pages.pair_form(error=True), status_code=401)
        with state.db() as conn:
            token = auth.add_device(conn, state.clock, name)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            DEVICE_COOKIE,
            token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=3600 * 24 * 365,
        )
        return resp

    # --- pages -------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def today() -> str:
        with state.db() as conn:
            return pages.today(state, conn, phone=lan)

    @app.get("/timeline", response_class=HTMLResponse)
    def timeline(day: str | None = None) -> str:
        with state.db() as conn:
            return pages.timeline(state, conn, day, phone=lan)

    @app.get("/projects", response_class=HTMLResponse)
    def projects(id: str | None = None) -> str:
        with state.db() as conn:
            if id:
                return pages.project(state, conn, id, phone=lan)
            return pages.projects(state, conn, phone=lan)

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(q: str = "") -> str:
        return pages.ask_page(q, phone=lan)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page() -> str:
        with state.db() as conn:
            return pages.inbox_page(state, conn)

    @app.get("/actions", response_class=HTMLResponse)
    def actions_page() -> str:
        with state.db() as conn:
            return pages.actions_page(state, conn)

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page() -> str:
        with state.db() as conn:
            return pages.memory_page(state, conn)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_page() -> str:
        with state.db() as conn:
            return pages.privacy_page(state, conn)

    # --- ask, pause, forget (also from a phone) -----------------------------------------------

    @app.post("/api/ask")
    def api_ask(body: AskBody, request: Request) -> dict[str, Any]:
        from personal_os.context.ask import ask

        fact = memory.parse_remember(body.question)
        if fact and not lan:
            return {"remember": fact, "text": f"Shall I remember: “{fact}”?"}
        key = viewer(request)
        previous = state.followups.get(key) if body.follow_up else None
        with state.db() as conn:
            answer = ask(
                conn, state.provider(), body.question, state.clock.now(), previous=previous
            )
            state.followups[key] = answer.question
            return pages.answer_json(conn, answer)

    @app.post("/api/pause")
    def api_pause(body: PauseBody) -> dict[str, str]:
        with state.db() as conn:
            duration = timedelta(minutes=body.minutes) if body.minutes else None
            marker = pause_mod.pause(
                conn, state.clock, duration=duration, runtime_dir=state.config.runtime_dir
            )
            build_pipeline(state.config, conn, state.clock).process([marker])
        until = f" for {body.minutes} minutes" if body.minutes else ""
        return {"message": f"Capture paused{until}."}

    @app.post("/api/resume")
    def api_resume() -> dict[str, str]:
        with state.db() as conn:
            marker = pause_mod.resume(conn, state.clock, runtime_dir=state.config.runtime_dir)
            build_pipeline(state.config, conn, state.clock).process([marker])
        return {"message": "Capture resumed."}

    @app.post("/api/forget")
    def api_forget(body: ForgetBody) -> dict[str, str]:
        end = state.clock.now()
        with state.db() as conn:
            start = end - timedelta(minutes=body.minutes)
            undo_dir = state.config.data_dir / "undo"
            report = maintenance.forget(conn, start, end, state.clock, undo_dir=undo_dir)
        return {"message": f"Forgot the last {body.minutes} minutes ({report.events} events)."}

    # --- laptop only ---------------------------------------------------------------------------

    @app.post("/api/inbox/assign")
    def api_assign(body: AssignBody) -> dict[str, str]:
        from personal_os.projects import inbox

        with state.db() as conn:
            group = inbox.InboxGroup(body.kind, body.key, event_ids=body.event_ids)
            n = inbox.assign_group(conn, state.clock, group, body.project)
        return {"message": f"Assigned {n} item(s) to {body.project} and learned it."}

    def executor(conn: sqlite3.Connection, confirm: Callable[[str], bool]) -> Executor:
        if state.desktop is None:
            raise HTTPException(503, "actions aren't available here")
        return Executor(
            state.config,
            conn,
            state.clock,
            build_registry(state.config),
            state.desktop(),
            confirm,
            provider=state.provider(),
        )

    def defer(_prompt: str) -> bool:
        raise Deferred

    @app.post("/api/do")
    def api_do(body: DoBody) -> dict[str, Any]:
        from personal_os.actions.audit import history
        from personal_os.actions.planner import run_request

        provider = state.provider()
        if provider is None:
            raise HTTPException(503, "the local model isn't available")
        with state.db() as conn:
            ex = executor(conn, defer)
            report = run_request(body.request, ex, provider)
            steps = []
            for out in report.outcomes:
                step: dict[str, Any] = {
                    "tool": out.tool,
                    "status": out.status,
                    "message": out.message,
                    "preview": out.preview,
                    "action": out.action_id[-8:],
                }
                if out.status == "pending":
                    first = history(conn, out.action_id)[0]
                    pid = out.action_id[-12:]
                    state.pending[pid] = PendingAction(
                        first.tool,
                        first.args,
                        first.tainted,
                        body.request,
                        out.preview,
                        first.tier,
                        state.clock.now(),
                    )
                    step["pending"] = pid
                    step["tainted"] = first.tainted
                steps.append(step)
        return {"reply": report.reply, "steps": steps, "error": report.error}

    @app.post("/api/actions/approve")
    def api_approve(body: PendingBody) -> dict[str, Any]:
        p = state.pending.pop(body.pending, None)
        if p is None or state.clock.now() - p.created > PENDING_TTL:
            raise HTTPException(410, "that request expired; ask again")
        with state.db() as conn:
            out = executor(conn, lambda _prompt: True).request(
                p.tool, p.args, origin=Origin.PLANNED, tainted=p.tainted, request_text=p.request
            )
        return {"message": f"{out.tool}: {out.message}", "status": out.status}

    @app.post("/api/actions/decline")
    def api_decline(body: PendingBody) -> dict[str, str]:
        state.pending.pop(body.pending, None)
        return {"message": "Declined."}

    @app.post("/api/actions/undo")
    def api_undo(body: UndoBody) -> dict[str, str]:
        from personal_os.actions.audit import find_action

        with state.db() as conn:
            full = find_action(conn, body.action)
            if full is None:
                raise HTTPException(404, "no such action")
            out = executor(conn, lambda _prompt: True).undo(full)
        return {"message": f"{out.tool}: {out.message}"}

    @app.post("/api/actions/kill")
    def api_kill() -> dict[str, str]:
        killswitch.engage(state.config.runtime_dir, state.clock.now(), "dashboard")
        killswitch.relock_all(state.config.runtime_dir)
        return {"message": "Kill switch on: nothing runs until you resume."}

    @app.post("/api/actions/resume")
    def api_actions_resume() -> dict[str, str]:
        killswitch.release(state.config.runtime_dir)
        return {"message": "Actions can run again."}

    @app.post("/api/actions/unlock")
    def api_unlock(body: UnlockBody) -> dict[str, str]:
        tool = build_registry().get(body.tool)
        if tool is None or int(tool.tier) != 3:
            raise HTTPException(400, "not a tier 3 tool")
        now = state.clock.now()
        killswitch.unlock(
            state.config.runtime_dir, body.tool, now + timedelta(minutes=body.minutes), now
        )
        return {"message": f"{body.tool} unlocked for {body.minutes} minutes; each use still asks."}

    @app.post("/api/actions/lock")
    def api_lock() -> dict[str, str]:
        killswitch.relock_all(state.config.runtime_dir)
        return {"message": "Tier 3 tools are blocked again."}

    @app.post("/api/memory/add")
    def api_memory_add(body: FactBody) -> dict[str, str]:
        with state.db() as conn:
            memory.add(conn, state.clock, body.text, "dashboard")
        return {"message": "Remembered."}

    @app.post("/api/memory/update")
    def api_memory_update(body: FactBody) -> dict[str, str]:
        with state.db() as conn:
            ok = body.id is not None and memory.update(conn, state.clock, body.id, body.text)
        if not ok:
            raise HTTPException(404, "no such fact")
        return {"message": "Updated."}

    @app.post("/api/memory/delete")
    def api_memory_delete(body: FactBody) -> dict[str, str]:
        with state.db() as conn:
            if body.id is None or not memory.delete(conn, body.id):
                raise HTTPException(404, "no such fact")
        return {"message": "Forgotten."}

    EDITABLE = {
        "observers.window.enabled",
        "observers.idle.enabled",
        "observers.fs.enabled",
        "observers.git.enabled",
        "observers.terminal.enabled",
        "observers.browser.enabled",
        "retention.raw_days",
        "retention.sensitive_days",
        "retention.page_text_days",
        "retention.terminal_output_days",
        "privacy.blocked_domains",
        "privacy.blocked_apps",
        "privacy.sensitive_domains",
        "privacy.no_text_domains",
        "llm.enabled",
        "llm.only_on_ac_power",
        "api.lan.enabled",
        "voice.enabled",
        "voice.wake_word_enabled",
        "voice.tts",
        "briefings.enabled",
        "briefings.speak",
    }

    @app.post("/api/settings")
    def api_settings(body: SettingBody) -> dict[str, str]:
        if body.key not in EDITABLE:
            raise HTTPException(400, "that setting can't be changed here")
        try:
            state.config = set_value(body.key, body.value, state.home)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"message": "Saved. Capture settings apply after a restart of capture."}

    @app.post("/api/briefing")
    def api_briefing() -> dict[str, Any]:
        from personal_os.context import briefing
        from personal_os.voice.main import google_client

        with state.db() as conn:
            local = state.clock.now().astimezone()
            kind: Any = (
                "evening" if f"{local:%H:%M}" >= state.config.briefings.evening_after else "morning"
            )
            b = briefing.build(conn, state.clock, kind, google=google_client(state.config))
        return {"title": b.title, "lines": b.lines, "notes": b.notes}

    @app.post("/api/setup/reviewed")
    def api_setup_reviewed() -> dict[str, str]:
        from personal_os.storage import repo

        with state.db() as conn:
            repo.set_setting(conn, "setup.reviewed", True, state.clock)
        return {"message": "Thanks. You can change sources any time under Privacy."}

    @app.post("/api/daemon/restart")
    def api_restart() -> dict[str, str]:
        if state.restart is None:
            raise HTTPException(503, "capture isn't running under the daemon")
        state.restart()
        return {"message": "Restarting capture; this page reconnects in a few seconds."}

    @app.get("/export/{fmt}")
    def export(fmt: str, project: str | None = None) -> Response:
        from personal_os.storage.export import export_json, export_markdown

        if fmt not in ("json", "markdown"):
            raise HTTPException(404)
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp, state.db() as conn:
            out = Path(tmp)
            if fmt == "json":
                export_json(conn, out, project)
            else:
                export_markdown(conn, out, project)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(out.iterdir()):
                    z.write(f, f.name)
        name = f"personal-os-{project or 'all'}-{fmt}-{state.clock.now():%Y%m%d}.zip"
        disposition = f'attachment; filename="{name}"'
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": disposition},
        )

    @app.post("/api/projects/delete")
    def api_delete_project(body: DeleteProjectBody) -> dict[str, str]:
        from personal_os.storage.export import delete_project_data

        if body.confirm != body.project:
            raise HTTPException(400, "type the project's id to confirm")
        with state.db() as conn:
            counts = delete_project_data(conn, body.project)
        return {
            "message": f"Deleted {counts.get('events', 0)} events and everything made from them."
        }

    @app.post("/api/lan/pair")
    def api_pair() -> dict[str, str]:
        if not state.config.api.lan.enabled:
            raise HTTPException(400, "turn on phone access first")
        code = state.pairing.start()
        return {"message": f"Pairing code {code} (5 minutes)", "code": code}

    @app.post("/api/lan/revoke")
    def api_revoke(body: DeviceBody) -> dict[str, str]:
        with state.db() as conn:
            if not auth.revoke(conn, body.device):
                raise HTTPException(404, "no such device")
        return {"message": "That phone can't connect any more."}

    return app
