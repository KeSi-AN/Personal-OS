// Browser control (ADR-076): commands from Donna arrive through the native host and run here.
// A few general operations, not one per site: tabs, open, read a page, act on it, media, and
// "open the first result" for site recipes kept as data on the Personal OS side.
//
// Limits on purpose: http(s) pages only (never brave:// or chrome://), never incognito, never
// type into or read password fields, and no acting on a page that shows a password field.

type Args = Record<string, unknown>;
export interface CmdResult {
  ok: boolean;
  data?: unknown;
  error?: string;
}

const TEXT_MAX = 6000;
const ELEMENTS_MAX = 80;

function web(url: string | undefined): boolean {
  return !!url && /^https?:\/\//i.test(url);
}

async function targetTab(args: Args): Promise<chrome.tabs.Tab> {
  let tab: chrome.tabs.Tab | undefined;
  if (typeof args.tabId === "number") tab = await chrome.tabs.get(args.tabId);
  else [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || tab.id === undefined) throw new Error("no tab to act on");
  if (tab.incognito) throw new Error("incognito tabs are off limits");
  if (!web(tab.url)) throw new Error("that tab isn't a web page");
  return tab;
}

async function focus(tab: chrome.tabs.Tab): Promise<void> {
  if (tab.id !== undefined) await chrome.tabs.update(tab.id, { active: true });
  if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
}

function waitComplete(tabId: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const done = (): void => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id: number, info: { status?: string }): void => {
      if (id === tabId && info.status === "complete") done();
    };
    const timer = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function inject<T>(tabId: number, func: (...a: never[]) => T, args: unknown[], main = false): Promise<T> {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: func as (...a: unknown[]) => T,
    args,
    world: main ? "MAIN" : "ISOLATED",
  });
  return res?.result as T;
}

// --- functions that run inside the page (self-contained: no closures) ---------------------

function readPage(textMax: number, elementsMax: number): unknown {
  const visible = (el: Element): boolean => {
    const r = (el as HTMLElement).getBoundingClientRect?.();
    const s = getComputedStyle(el);
    return !!r && r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const labelOf = (el: Element): string => {
    const e = el as HTMLInputElement;
    const t =
      e.getAttribute("aria-label") || e.getAttribute("title") || (e as HTMLElement).innerText ||
      e.getAttribute("placeholder") || (e.type === "submit" || e.type === "button" ? e.value : "") ||
      e.getAttribute("alt") || e.getAttribute("name") || "";
    return t.replace(/\s+/g, " ").trim().slice(0, 80);
  };
  const sel = 'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [contenteditable="true"]';
  const elements: { ref: number; role: string; label: string }[] = [];
  let ref = 0;
  for (const el of Array.from(document.querySelectorAll(sel))) {
    if (elements.length >= elementsMax) break;
    if ((el as HTMLInputElement).type === "password" || !visible(el)) continue;
    const label = labelOf(el);
    if (!label) continue;
    ref += 1;
    el.setAttribute("data-pos-ref", String(ref));
    const role = el.getAttribute("role") || (el.tagName === "A" ? "link" : el.tagName === "INPUT" ? `input:${(el as HTMLInputElement).type || "text"}` : el.tagName.toLowerCase());
    elements.push({ ref, role, label });
  }
  const main = (document.querySelector("main, article, [role=main]") as HTMLElement | null) ?? document.body;
  return {
    title: document.title,
    url: location.href,
    text: (main?.innerText ?? "").replace(/\n{3,}/g, "\n\n").slice(0, textMax),
    hasPassword: !!document.querySelector('input[type="password"]'),
    elements,
  };
}

function actOnPage(spec: { ref?: number; label?: string; action: string; text?: string; key?: string }): unknown {
  if (document.querySelector('input[type="password"]')) return { ok: false, error: "this page has a password field: not touching it" };
  const sel = 'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [contenteditable="true"]';
  let el: HTMLElement | null = null;
  if (spec.ref) el = document.querySelector(`[data-pos-ref="${spec.ref}"]`);
  if (!el && spec.label) {
    const want = spec.label.toLowerCase().trim();
    const all = Array.from(document.querySelectorAll(sel)) as HTMLElement[];
    const text = (e: HTMLElement): string =>
      (e.getAttribute("aria-label") || e.getAttribute("title") || e.innerText || e.getAttribute("placeholder") || "").toLowerCase().replace(/\s+/g, " ").trim();
    el = all.find((e) => text(e) === want) ?? all.find((e) => text(e).includes(want)) ?? null;
  }
  if (spec.action === "scroll") {
    window.scrollBy({ top: (spec.text === "up" ? -1 : 1) * window.innerHeight * 0.8, behavior: "smooth" });
    return { ok: true, data: "scrolled" };
  }
  if (!el) return { ok: false, error: "couldn't find that on the page" };
  if ((el as HTMLInputElement).type === "password") return { ok: false, error: "not a password field" };
  el.scrollIntoView({ block: "center" });
  const name = (el.getAttribute("aria-label") || el.innerText || el.getAttribute("placeholder") || "").trim().slice(0, 60);
  if (spec.action === "click") {
    el.click();
    return { ok: true, data: `clicked "${name}"` };
  }
  if (spec.action === "type") {
    el.focus();
    if (el.isContentEditable) document.execCommand("insertText", false, spec.text ?? "");
    else {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, spec.text ?? "");
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return { ok: true, data: `typed into "${name}"` };
  }
  if (spec.action === "press") {
    const key = spec.key || "Enter";
    el.focus();
    for (const type of ["keydown", "keypress", "keyup"]) el.dispatchEvent(new KeyboardEvent(type, { key, code: key, bubbles: true }));
    const form = (el as HTMLInputElement).form;
    if (key === "Enter" && form) form.requestSubmit();
    return { ok: true, data: `pressed ${key}` };
  }
  return { ok: false, error: `unknown action ${spec.action}` };
}

function mediaControl(action: string, seconds: number): unknown {
  const media = Array.from(document.querySelectorAll("video, audio")) as HTMLMediaElement[];
  const m = media.find((x) => !x.paused) ?? media.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
  const meta = (navigator as Navigator & { mediaSession?: MediaSession }).mediaSession?.metadata;
  const status = (): unknown => ({
    title: meta?.title || document.title,
    artist: meta?.artist || "",
    album: meta?.album || "",
    playing: !!m && !m.paused,
    position: m ? Math.round(m.currentTime) : null,
    duration: m && isFinite(m.duration) ? Math.round(m.duration) : null,
  });
  const button = (sels: string[]): HTMLElement | null => {
    for (const s of sels) {
      const b = document.querySelector(s) as HTMLElement | null;
      if (b) return b;
    }
    return null;
  };
  if (action === "status") return { ok: true, data: status() };
  if (action === "next" || action === "previous") {
    const b = button(action === "next"
      ? [".ytp-next-button", "ytmusic-player-bar .next-button", '[data-testid="control-button-skip-forward"]', '[aria-label="Next"]', '[aria-label="Next track"]']
      : ["ytmusic-player-bar .previous-button", '[data-testid="control-button-skip-back"]', '[aria-label="Previous"]', '[aria-label="Previous track"]']);
    if (!b) return { ok: false, error: `no ${action} button on this page` };
    b.click();
    return { ok: true, data: status() };
  }
  if (!m) return { ok: false, error: "nothing playable on this page" };
  if (action === "play") void m.play();
  else if (action === "pause") m.pause();
  else if (action === "toggle") void (m.paused ? m.play() : m.pause());
  else if (action === "seek") m.currentTime = Math.max(0, m.currentTime + seconds);
  else return { ok: false, error: `unknown media action ${action}` };
  return { ok: true, data: status() };
}

function firstMatch(selector: string, timeoutMs: number): Promise<string | null> {
  return new Promise((resolve) => {
    const started = Date.now();
    const tick = (): void => {
      const a = document.querySelector(selector) as HTMLAnchorElement | null;
      if (a?.href) resolve(a.href);
      else if (Date.now() - started > timeoutMs) resolve(null);
      else setTimeout(tick, 200);
    };
    tick();
  });
}

// --- commands ------------------------------------------------------------------------------

export async function runCommand(op: string, args: Args): Promise<CmdResult> {
  try {
    switch (op) {
      case "tabs.list": {
        const tabs = await chrome.tabs.query({});
        return {
          ok: true,
          data: tabs.filter((t) => !t.incognito && web(t.url)).map((t) => ({
            id: t.id, title: t.title, url: t.url, active: t.active, audible: !!t.audible,
            muted: !!t.mutedInfo?.muted, pinned: t.pinned, window: t.windowId,
            lastAccessed: (t as chrome.tabs.Tab & { lastAccessed?: number }).lastAccessed ?? null,
          })),
        };
      }
      case "tabs.open": {
        const url = String(args.url ?? "");
        if (!web(url)) throw new Error("only http(s) addresses");
        let tab: chrome.tabs.Tab;
        if (args.newTab === false) {
          const cur = await targetTab({});
          tab = (await chrome.tabs.update(cur.id!, { url, active: true })) ?? cur;
        } else tab = await chrome.tabs.create({ url, active: true });
        await focus(tab);
        return { ok: true, data: { id: tab.id, url } };
      }
      case "tabs.activate": {
        const tab = await targetTab(args);
        await focus(tab);
        return { ok: true, data: { id: tab.id, title: tab.title } };
      }
      case "tabs.close": {
        const ids = (args.tabIds as number[] | undefined) ?? [(await targetTab(args)).id!];
        await chrome.tabs.remove(ids);
        return { ok: true, data: { closed: ids.length } };
      }
      case "tabs.nav": {
        const tab = await targetTab(args);
        const a = String(args.action);
        if (a === "back") await chrome.tabs.goBack(tab.id!);
        else if (a === "forward") await chrome.tabs.goForward(tab.id!);
        else if (a === "reload") await chrome.tabs.reload(tab.id!);
        else throw new Error(`unknown navigation ${a}`);
        return { ok: true, data: a };
      }
      case "tabs.mute": {
        const tab = await targetTab(args);
        await chrome.tabs.update(tab.id!, { muted: args.muted !== false });
        return { ok: true, data: { muted: args.muted !== false } };
      }
      case "page.read": {
        const tab = await targetTab(args);
        return { ok: true, data: await inject(tab.id!, readPage as never, [TEXT_MAX, ELEMENTS_MAX]) };
      }
      case "page.act": {
        const tab = await targetTab(args);
        const res = (await inject(tab.id!, actOnPage as never, [args])) as CmdResult;
        return res ?? { ok: false, error: "the page didn't answer" };
      }
      case "media": {
        let tab: chrome.tabs.Tab | undefined;
        if (typeof args.tabId !== "number") {
          // The tab making sound, else the active one: "pause it" means what's playing.
          [tab] = await chrome.tabs.query({ audible: true });
        }
        tab = tab && !tab.incognito ? tab : await targetTab(args);
        const res = (await inject(tab.id!, mediaControl as never, [String(args.action ?? "toggle"), Number(args.seconds ?? 0)], true)) as CmdResult;
        return res ?? { ok: false, error: "the page didn't answer" };
      }
      case "open_first": {
        // A site recipe: open a search page, take the first result matching `selector`, open it.
        const url = String(args.url ?? "");
        if (!web(url)) throw new Error("only http(s) addresses");
        const tab = await chrome.tabs.create({ url, active: true });
        await focus(tab);
        await waitComplete(tab.id!, 10000);
        const href = await inject(tab.id!, firstMatch as never, [String(args.selector), 8000]);
        if (!href || !web(href as string)) return { ok: false, error: "no result found on the page" };
        await chrome.tabs.update(tab.id!, { url: href as string });
        await waitComplete(tab.id!, 10000);
        const t = await chrome.tabs.get(tab.id!);
        return { ok: true, data: { id: t.id, title: t.title, url: t.url } };
      }
      default:
        return { ok: false, error: `unknown command ${op}` };
    }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}
