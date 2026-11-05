// Service worker (Manifest V3). Tracks visits per tab and their active dwell, and sends them to
// the Personal OS native host. MV3 can stop this worker at any time, so all state lives in
// chrome.storage.session and every handler loads/saves it.

import {
  type FocusState,
  type IdleState,
  type Visit,
  isTrackable,
  newVisit,
  pushBounded,
  syncActive,
  toRecord,
} from "./visit";

const HOST = "com.personalos.host";
const BROWSER = navigator.userAgent.includes("Edg/") ? "edge" : "chrome"; // Brave reports as Chrome
const OUTBOX_MAX = 1000;
const CHECKPOINT_ALARM = "checkpoint";

interface State extends FocusState {
  visits: Record<string, Visit>;
  paused: boolean;
}

const EMPTY: State = { visits: {}, activeTab: null, windowFocused: true, idle: "active", paused: false };

// --- state (serialised so concurrent events don't overwrite each other) ---------------------

let chain: Promise<unknown> = Promise.resolve();

function withState(fn: (st: State) => void | Promise<void>): Promise<void> {
  const run = chain.then(async () => {
    const { state } = await chrome.storage.session.get("state");
    const st: State = { ...EMPTY, ...(state as State | undefined) };
    await fn(st);
    await chrome.storage.session.set({ state: st });
  });
  chain = run.catch((err) => console.warn("personal-os:", err));
  return run;
}

// --- native host --------------------------------------------------------------------------

let port: chrome.runtime.Port | null = null;

function setBadge(text: string): void {
  void chrome.action.setBadgeText({ text });
  void chrome.action.setTitle({
    title: text === "!" ? "Personal OS: not connected" : text === "II" ? "Personal OS: paused" : "Personal OS",
  });
}

function connect(): chrome.runtime.Port | null {
  if (port) return port;
  try {
    const p = chrome.runtime.connectNative(HOST);
    p.onMessage.addListener((msg: { paused?: boolean }) => {
      if (typeof msg?.paused === "boolean") {
        setBadge(msg.paused ? "II" : "");
        void withState((st) => {
          st.paused = msg.paused === true;
        });
      }
    });
    p.onDisconnect.addListener(() => {
      port = null;
      setBadge("!");
    });
    p.postMessage({ type: "hello", version: chrome.runtime.getManifest().version });
    port = p;
    void flushOutbox();
    return p;
  } catch {
    setBadge("!");
    return null;
  }
}

async function flushOutbox(): Promise<void> {
  const { outbox } = await chrome.storage.local.get("outbox");
  const items = (outbox as object[] | undefined) ?? [];
  if (!items.length || !port) return;
  await chrome.storage.local.set({ outbox: [] });
  for (const item of items) send(item);
}

function send(record: object): void {
  const p = connect();
  try {
    if (!p) throw new Error("no host");
    p.postMessage(record);
  } catch {
    void chrome.storage.local.get("outbox").then(({ outbox }) =>
      chrome.storage.local.set({ outbox: pushBounded((outbox as object[]) ?? [], record, OUTBOX_MAX) }),
    );
  }
}

// --- visits -------------------------------------------------------------------------------

/** Same page for the purpose of attaching a content report: same origin, path and main query
 *  parameter. Single-page apps (YouTube) rewrite the URL with extra parameters after loading. */
function samePage(a: string | undefined, b: string): boolean {
  if (!a) return false;
  try {
    const x = new URL(a);
    const y = new URL(b);
    return x.origin === y.origin && x.pathname === y.pathname && x.searchParams.get("v") === y.searchParams.get("v");
  } catch {
    return false;
  }
}

function finish(st: State, tabId: number, now: number): void {
  const v = st.visits[tabId];
  if (!v) return;
  syncActive([v], { ...st, activeTab: null }, now); // stop its clock
  if (!st.paused) send(toRecord(v, now, now, BROWSER));
  delete st.visits[tabId];
}

async function start(
  st: State,
  tabId: number,
  url: string,
  transition: string | null,
  qualifiers: string[],
): Promise<void> {
  const now = Date.now();
  const previous = st.visits[tabId] ?? null;
  finish(st, tabId, now);
  let tab: chrome.tabs.Tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return; // tab already gone
  }
  if (tab.incognito || !isTrackable(url)) return;
  // A page opened in the tab that's already in front never fires onActivated: learn it here.
  if (tab.active) st.activeTab = tabId;
  const opener = tab.openerTabId !== undefined ? (st.visits[tab.openerTabId] ?? null) : null;
  st.visits[tabId] = newVisit({
    id: crypto.randomUUID(),
    tabId,
    windowId: tab.windowId,
    url,
    title: tab.title ?? "",
    now,
    transition,
    qualifiers,
    previous,
    opener,
  });
  syncActive(Object.values(st.visits), st, now);
}

chrome.webNavigation.onCommitted.addListener((d) => {
  if (d.frameId !== 0) return;
  void withState((st) => start(st, d.tabId, d.url, d.transitionType, d.transitionQualifiers));
});

chrome.webNavigation.onHistoryStateUpdated.addListener((d) => {
  // Single-page apps change the URL without a page load.
  if (d.frameId !== 0) return;
  void withState(async (st) => {
    if (st.visits[d.tabId]?.url === d.url) return;
    await start(st, d.tabId, d.url, d.transitionType, d.transitionQualifiers);
  }).then(() => chrome.tabs.sendMessage(d.tabId, { kind: "reextract" }).catch(() => undefined));
});

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.title === undefined) return;
  void withState((st) => {
    const v = st.visits[tabId];
    if (v) v.title = change.title ?? v.title;
  });
});

// Address-bar navigations are often pre-rendered in a hidden tab that then replaces the visible
// one under a new id; onActivated doesn't fire for it, so carry "in front" over here.
chrome.tabs.onReplaced.addListener((addedTabId, removedTabId) =>
  void withState((st) => {
    const now = Date.now();
    if (st.activeTab === removedTabId) st.activeTab = addedTabId;
    finish(st, removedTabId, now);
    syncActive(Object.values(st.visits), st, now);
  }),
);

chrome.tabs.onRemoved.addListener((tabId) => void withState((st) => finish(st, tabId, Date.now())));

chrome.tabs.onActivated.addListener(({ tabId }) =>
  void withState((st) => {
    st.activeTab = tabId;
    syncActive(Object.values(st.visits), st, Date.now());
  }),
);

chrome.windows.onFocusChanged.addListener((windowId) =>
  void withState(async (st) => {
    st.windowFocused = windowId !== chrome.windows.WINDOW_ID_NONE;
    if (st.windowFocused) {
      const [tab] = await chrome.tabs.query({ active: true, windowId });
      st.activeTab = tab?.id ?? null;
    }
    syncActive(Object.values(st.visits), st, Date.now());
  }),
);

chrome.idle.onStateChanged.addListener((state: IdleState) =>
  void withState((st) => {
    st.idle = state;
    syncActive(Object.values(st.visits), st, Date.now());
  }),
);

chrome.bookmarks.onCreated.addListener((_id, node) => {
  if (!node.url || !isTrackable(node.url)) return;
  void withState((st) => {
    for (const v of Object.values(st.visits)) if (v.url === node.url) v.bookmarked = true;
    if (!st.paused) send({ v: 1, type: "signal", kind: "bookmark_added", url: node.url, at: new Date().toISOString() });
  });
});

// Content-script reports: page extract, password field, scroll, video, selection/copy.
chrome.runtime.onMessage.addListener((msg, sender) => {
  const tabId = sender.tab?.id;
  if (tabId === undefined || sender.tab?.incognito) return;
  void withState((st) => {
    const v = st.visits[tabId];
    if (msg?.kind === "page" && v && samePage(sender.url, v.url)) {
      if (msg.hasPassword) v.hasPassword = true;
      if (msg.extract && !v.hasPassword) v.extract = msg.extract;
      if (typeof msg.scrollMax === "number") v.scrollMax = Math.max(v.scrollMax ?? 0, msg.scrollMax);
      if (typeof msg.videoS === "number") v.videoS = Math.max(v.videoS, msg.videoS);
      if (typeof msg.referrer === "string" && msg.referrer) v.referrer = msg.referrer;
      if (msg.hints && typeof msg.hints === "object") v.hints = { ...v.hints, ...msg.hints };
    } else if (msg?.kind === "signal" && (msg.signal === "selection" || msg.signal === "copy")) {
      if (st.paused || v?.hasPassword) return;
      send({ v: 1, type: "signal", kind: msg.signal, url: sender.url, text: String(msg.text ?? "").slice(0, 500), at: new Date().toISOString() });
    }
  });
});

// Open visits are re-sent about once a minute (same id) so a browser crash loses little.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== CHECKPOINT_ALARM) return;
  void withState((st) => {
    if (st.paused) return;
    const now = Date.now();
    for (const v of Object.values(st.visits)) send(toRecord(v, now, null, BROWSER));
  });
  void sendTabs();
});

// --- open tabs: what's open right now, for "what tabs do I have open?" ----------------------
// Sent when tabs change (debounced) and once a minute. The host keeps only the latest list,
// never history. Incognito tabs and non-web pages are never sent.
const TABS_MAX = 100;
let tabsTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleTabs(): void {
  if (tabsTimer !== null) clearTimeout(tabsTimer);
  tabsTimer = setTimeout(() => void sendTabs(), 1500);
}

async function sendTabs(): Promise<void> {
  tabsTimer = null;
  const tabs = await chrome.tabs.query({});
  const list = tabs
    .filter((t) => !t.incognito && isTrackable(t.url))
    .slice(0, TABS_MAX)
    .map((t) => ({
      title: (t.title ?? "").slice(0, 200),
      url: t.url,
      active: t.active,
      audible: t.audible === true,
      window: t.windowId,
    }));
  await withState((st) => {
    if (!st.paused) send({ v: 1, type: "tabs", at: new Date().toISOString(), tabs: list });
  });
}

chrome.tabs.onCreated.addListener(() => scheduleTabs());
chrome.tabs.onRemoved.addListener(() => scheduleTabs());
chrome.tabs.onActivated.addListener(() => scheduleTabs());
chrome.tabs.onUpdated.addListener((_id, change) => {
  if (change.title !== undefined || change.url !== undefined || change.audible !== undefined) scheduleTabs();
});
chrome.windows.onRemoved.addListener(() => scheduleTabs());

async function initFocus(): Promise<void> {
  // The worker may start long after the browser: read the real focus state, don't assume it.
  const [win, idle] = await Promise.all([
    chrome.windows.getLastFocused().catch(() => undefined),
    chrome.idle.queryState(60),
  ]);
  const [tab] = win?.id !== undefined ? await chrome.tabs.query({ active: true, windowId: win.id }) : [];
  await withState((st) => {
    st.windowFocused = !!win?.focused;
    st.activeTab = tab?.id ?? st.activeTab;
    st.idle = idle;
    syncActive(Object.values(st.visits), st, Date.now());
  });
}

function boot(): void {
  chrome.idle.setDetectionInterval(60);
  void chrome.alarms.create(CHECKPOINT_ALARM, { periodInMinutes: 1 });
  connect();
  void initFocus();
  scheduleTabs();
}

chrome.runtime.onStartup.addListener(boot);
chrome.runtime.onInstalled.addListener(boot);
boot();
