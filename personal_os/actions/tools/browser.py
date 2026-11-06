"""Browser control through the extension (ADR-076). A few general tools, not one per site:

    browser_open   a page or a search (google, youtube, youtube music, github ...)    tier 1
    play_media     find a song or video by name and start it                          tier 1
    browser_tab    switch, close, back, forward, reload, mute                          tier 1
    browser_media  play, pause, next, previous, seek what's playing in the browser     tier 1
    browser_read   the current page's text and its buttons, links and fields           tier 0
    whats_playing  the song or video playing in the browser                            tier 0
    browser_act    click, type, press a key, scroll on the page                        tier 2

Reading is captured content (it taints what follows), so a click after a read needs a yes.
Pages with a password field, incognito tabs and non-web pages are refused by the extension.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.browser import control, recipes


def _send(ctx: ToolContext, op: str, args: dict[str, Any] | None = None) -> Any:
    try:
        return control.send(ctx.config.runtime_dir, op, args, browser=ctx.config.actions.browser)
    except (control.BrowserUnavailable, control.BrowserError) as exc:
        raise ActionError(str(exc)) from None


def _site_names() -> str:
    return ", ".join(s.name for s in recipes.SITES)


class OpenArgs(Args):
    url: str | None = Field(None, max_length=2000, description="a full http(s) address")
    query: str | None = Field(None, max_length=300, description="what to search for")
    site: str = Field("google", max_length=40, description="where to search")
    new_tab: bool = True


class BrowserOpen(Tool):
    name = "browser_open"
    summary = (
        "open a web page, or search a site for something, in the browser "
        f"(sites: {_site_names()}); for anything on the web, prefer this to open_url"
    )
    tier = Tier.LOW
    network = True
    args_model = OpenArgs

    def _url(self, args: OpenArgs) -> str:
        if args.url:
            if not args.url.startswith(("http://", "https://")):
                return "https://" + args.url
            return args.url
        if not args.query:
            raise ActionError("give a web address or something to search for")
        site = recipes.find(args.site) or recipes.find("google")
        assert site is not None
        return recipes.search_url(site, args.query)

    def preview(self, args: OpenArgs, ctx: ToolContext) -> str:
        return f"open {self._url(args)} in the browser"

    def run(self, args: OpenArgs, ctx: ToolContext) -> Result:
        url = self._url(args)
        _send(ctx, "tabs.open", {"url": url, "newTab": args.new_tab})
        what = f'a {args.site} search for "{args.query}"' if args.query and not args.url else url
        return Result(f"Opened {what}")


class PlayArgs(Args):
    query: str = Field(min_length=1, max_length=200, description="song, artist or video")
    site: Literal["youtube music", "youtube"] = "youtube music"


class PlayMedia(Tool):
    name = "play_media"
    summary = (
        "play a specific song or video by name: finds it on YouTube Music (music) or YouTube "
        "(videos) in the browser and starts it"
    )
    tier = Tier.LOW
    network = True
    args_model = PlayArgs

    def preview(self, args: PlayArgs, ctx: ToolContext) -> str:
        return f'play "{args.query}" on {args.site}'

    def run(self, args: PlayArgs, ctx: ToolContext) -> Result:
        site = recipes.find(args.site)
        assert site is not None and site.first
        data = _send(
            ctx, "open_first", {"url": recipes.search_url(site, args.query), "selector": site.first}
        )
        title = str((data or {}).get("title") or args.query).replace(" - YouTube Music", "")
        title = title.replace(" - YouTube", "")
        return Result(f'Playing "{title}" on {args.site}', data=data)


class TabArgs(Args):
    action: Literal["switch", "close", "back", "forward", "reload", "mute", "unmute"]
    which: str = Field(
        "", max_length=80, description="words from the tab's title or site; empty = the current tab"
    )
    all: bool = Field(False, description="close: every tab matching `which`")


class BrowserTab(Tool):
    name = "browser_tab"
    summary = "switch to, close, go back/forward in, reload, mute or unmute a browser tab"
    tier = Tier.LOW
    args_model = TabArgs

    def _matches(self, ctx: ToolContext, which: str) -> list[dict[str, Any]]:
        tabs = _send(ctx, "tabs.list") or []
        if not which.strip():
            return [t for t in tabs if t.get("active")][:1]
        w = which.lower().strip()
        return [
            t
            for t in tabs
            if w in str(t.get("title", "")).lower() or w in str(t.get("url", "")).lower()
        ]

    def preview(self, args: TabArgs, ctx: ToolContext) -> str:
        scope = "all tabs matching" if args.all else "tab"
        return f"{args.action} {scope} {args.which or '(current)'}"

    def run(self, args: TabArgs, ctx: ToolContext) -> Result:
        found = self._matches(ctx, args.which)
        if not found:
            raise ActionError(
                f'no open tab matches "{args.which}"' if args.which else "no current tab"
            )
        first = found[0]
        if args.action == "switch":
            _send(ctx, "tabs.activate", {"tabId": first["id"]})
            return Result(f'Switched to "{first.get("title", "")}"')
        if args.action == "close":
            ids = [t["id"] for t in (found if args.all else found[:1])]
            _send(ctx, "tabs.close", {"tabIds": ids})
            return Result(f"Closed {len(ids)} tab{'s' if len(ids) != 1 else ''}")
        if args.action in ("mute", "unmute"):
            _send(ctx, "tabs.mute", {"tabId": first["id"], "muted": args.action == "mute"})
            return Result(f'{args.action.capitalize()}d "{first.get("title", "")}"')
        nav = {"back": "back", "forward": "forward", "reload": "reload"}[args.action]
        _send(ctx, "tabs.nav", {"tabId": first["id"], "action": nav})
        return Result(f"Went {nav}" if nav != "reload" else "Reloaded the page")


class MediaArgs(Args):
    action: Literal["play", "pause", "toggle", "next", "previous", "seek"]
    seconds: int = Field(0, ge=-3600, le=3600, description="seek: forward (+) or back (-)")


class BrowserMedia(Tool):
    name = "browser_media"
    summary = (
        "control the song or video already playing in the browser: play, pause, next, previous, "
        "seek (it can't choose a song: use play_media for that)"
    )
    tier = Tier.LOW
    args_model = MediaArgs

    def preview(self, args: MediaArgs, ctx: ToolContext) -> str:
        return f"{args.action} the media in the browser"

    def run(self, args: MediaArgs, ctx: ToolContext) -> Result:
        status = _send(ctx, "media", {"action": args.action, "seconds": args.seconds}) or {}
        what = " - ".join(x for x in (status.get("title"), status.get("artist")) if x)
        state = "playing" if status.get("playing") else "paused"
        return Result(f"{args.action.capitalize()}: {what or 'the media'} is now {state}")


class NoArgs(Args):
    pass


class WhatsPlaying(Tool):
    name = "whats_playing"
    summary = "the song or video playing in the browser right now (title, artist, position)"
    tier = Tier.READ
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "look at what's playing in the browser"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        s = _send(ctx, "media", {"action": "status"}) or {}
        what = " by ".join(x for x in (s.get("title"), s.get("artist")) if x) or "nothing"
        state = "playing" if s.get("playing") else "paused"
        return Result(f"{what} ({state})", data=s, captured=True)


class ReadArgs(Args):
    which: str = Field(
        "", max_length=80, description="words from the tab's title; empty = the current tab"
    )


class BrowserRead(Tool):
    name = "browser_read"
    summary = (
        "read the current browser page (or a tab matching `which`): its text, and its buttons, "
        "links and fields, numbered for browser_act"
    )
    tier = Tier.READ
    args_model = ReadArgs

    def preview(self, args: ReadArgs, ctx: ToolContext) -> str:
        return f"read the page {args.which or '(current tab)'}"

    def run(self, args: ReadArgs, ctx: ToolContext) -> Result:
        params: dict[str, Any] = {}
        if args.which:
            found = BrowserTab()._matches(ctx, args.which)
            if not found:
                raise ActionError(f'no open tab matches "{args.which}"')
            params["tabId"] = found[0]["id"]
        page = _send(ctx, "page.read", params) or {}
        elements = "\n".join(
            f"[{e['ref']}] {e['role']}: {e['label']}" for e in page.get("elements", [])
        )
        text = f"{page.get('title', '')}\n{page.get('url', '')}\n\n{page.get('text', '')}"
        if elements:
            text += f"\n\nOn the page (use the number with browser_act):\n{elements}"
        return Result(f'read "{page.get("title", "")}"', data=text, captured=True)


class ActArgs(Args):
    action: Literal["click", "type", "press", "scroll"]
    ref: int | None = Field(None, ge=1, description="the element's number from browser_read")
    label: str = Field("", max_length=80, description="or the button/link/field's label")
    text: str = Field("", max_length=500, description="type: the text; scroll: up or down")
    key: str = Field("Enter", max_length=20)


class BrowserAct(Tool):
    name = "browser_act"
    summary = (
        "click a button or link, type into a field, press a key or scroll, on the current "
        "browser page (find elements with browser_read first)"
    )
    tier = Tier.CONFIRM
    args_model = ActArgs

    def preview(self, args: ActArgs, ctx: ToolContext) -> str:
        target = f"#{args.ref}" if args.ref else f'"{args.label}"'
        if args.action == "type":
            return f'type "{args.text}" into {target} on the current page'
        if args.action == "scroll":
            return f"scroll {args.text or 'down'}"
        return f"{args.action} {target} on the current page"

    def run(self, args: ActArgs, ctx: ToolContext) -> Result:
        if args.action != "scroll" and not args.ref and not args.label:
            raise ActionError("say which button, link or field")
        spec = {
            "action": args.action,
            "ref": args.ref,
            "label": args.label,
            "text": args.text,
            "key": args.key,
        }
        done = _send(ctx, "page.act", spec)
        return Result(str(done or "done").capitalize())
