// Pure visit bookkeeping (no chrome.* calls), so it can be unit-tested with node:test.
//
// Active dwell counts only while the tab is the active tab of the focused browser window AND
// the user is not idle (chrome.idle). Times are epoch milliseconds.

export type IdleState = "active" | "idle" | "locked";

export interface PageExtract {
  title: string;
  metaDescription: string;
  headings: string[];
  text: string;
  wordCount: number;
  lang: string | null;
}

export interface Visit {
  id: string;
  tabId: number;
  windowId: number;
  url: string;
  title: string;
  start: number;
  activeMs: number;
  activeSince: number | null;
  transition: string | null;
  qualifiers: string[];
  referrer: string | null;
  openerVisitId: string | null;
  chainId: string;
  bookmarked: boolean;
  hasPassword: boolean;
  scrollMax: number | null;
  videoS: number;
  extract: PageExtract | null;
  hints: Record<string, string | boolean>;
}

export interface FocusState {
  activeTab: number | null;
  windowFocused: boolean;
  idle: IdleState;
}

export function isTrackable(url: string | undefined): url is string {
  return !!url && /^https?:\/\//i.test(url);
}

export function shouldBeActive(v: Visit, f: FocusState): boolean {
  return v.tabId === f.activeTab && f.windowFocused && f.idle === "active";
}

/** Start or stop each visit's active clock to match the focus state. */
export function syncActive(visits: Iterable<Visit>, f: FocusState, now: number): void {
  for (const v of visits) {
    const want = shouldBeActive(v, f);
    if (want && v.activeSince === null) {
      v.activeSince = now;
    } else if (!want && v.activeSince !== null) {
      v.activeMs += Math.max(0, now - v.activeSince);
      v.activeSince = null;
    }
  }
}

export function activeMsAt(v: Visit, now: number): number {
  return v.activeMs + (v.activeSince !== null ? Math.max(0, now - v.activeSince) : 0);
}

export function newVisit(args: {
  id: string;
  tabId: number;
  windowId: number;
  url: string;
  title: string;
  now: number;
  transition: string | null;
  qualifiers: string[];
  previous: Visit | null; // the visit this tab showed before (same-tab navigation)
  opener: Visit | null; // the visit in the tab that opened this one
}): Visit {
  // A visit chain follows link-clicks within a tab and into tabs opened from it; typing a URL
  // or searching from the address bar starts a new chain.
  const fresh = args.transition === "typed" || args.transition === "generated";
  const parent = args.previous ?? args.opener;
  return {
    id: args.id,
    tabId: args.tabId,
    windowId: args.windowId,
    url: args.url,
    title: args.title,
    start: args.now,
    activeMs: 0,
    activeSince: null,
    transition: args.transition,
    qualifiers: args.qualifiers,
    referrer: null,
    openerVisitId: args.opener?.id ?? null,
    chainId: !fresh && parent ? parent.chainId : args.id,
    bookmarked: false,
    hasPassword: false,
    scrollMax: null,
    videoS: 0,
    extract: null,
    hints: {},
  };
}

/** The record sent to the native host. `end` null means the visit is still open. */
export function toRecord(v: Visit, now: number, end: number | null, browser: string) {
  return {
    v: 1,
    type: "visit",
    id: v.id,
    browser,
    start: new Date(v.start).toISOString(),
    end: end === null ? null : new Date(end).toISOString(),
    last_seen: new Date(now).toISOString(),
    url: v.url,
    title: v.title,
    tabId: v.tabId,
    windowId: v.windowId,
    transition: v.transition,
    qualifiers: v.qualifiers,
    referrer: v.referrer,
    openerVisitId: v.openerVisitId,
    chainId: v.chainId,
    activeMs: Math.round(activeMsAt(v, end ?? now)),
    scrollMax: v.scrollMax,
    videoS: Math.round(v.videoS),
    bookmarked: v.bookmarked,
    // Never send page text from a page that had a password or payment field.
    hasPassword: v.hasPassword,
    extract: v.hasPassword ? null : v.extract,
    hints: v.hasPassword ? {} : v.hints,
    incognito: false,
  };
}

/** Bounded outbox for when the native host is unreachable (oldest dropped first). */
export function pushBounded<T>(queue: T[], item: T, max: number): T[] {
  queue.push(item);
  return queue.length > max ? queue.slice(queue.length - max) : queue;
}

const words = (s: string) => (s ? s.split(" ").length : 0);

/** Main text: Readability when it finds a real article; otherwise the page's <main>/<article>;
 *  otherwise the body. Readability sometimes settles on a footer or sidebar (a few dozen
 *  words) on sites with unusual markup, so a short result loses to a much longer main area. */
export function pickMainText(readable: string, main: string, body: string): string {
  if (words(readable) >= 150) return readable;
  const best = words(main) >= words(readable) * 2 && words(main) >= 50 ? main : readable;
  if (words(best) >= 50) return best;
  return words(body) > words(best) ? body : best;
}

