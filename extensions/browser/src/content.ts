// Content script: reads a compact, readable extract of the page (never raw HTML), notices
// password/payment fields, and measures scroll depth and video watch time. Text you select or
// copy is sent only from ordinary page content, never from form fields.

import { Readability } from "@mozilla/readability";

const MAX_TEXT = 3000;
const SENSITIVE_FIELDS =
  'input[type="password"], input[autocomplete="cc-number"], input[autocomplete="cc-csc"], ' +
  'input[autocomplete="cc-exp"], input[name*="cardnumber" i], input[autocomplete="one-time-code"]';

let sensitive = false;
let scrollMax = 0;
let videoSeconds = 0;
const playingSince = new WeakMap<EventTarget, number>();
let dirty = false;
let lastSelection = "";

function report(msg: object): void {
  try {
    void chrome.runtime.sendMessage({ kind: "page", ...msg }).catch(() => undefined);
  } catch {
    // extension reloaded: this script is orphaned and stays quiet
  }
}

function checkSensitive(): boolean {
  if (!sensitive && document.querySelector(SENSITIVE_FIELDS)) {
    sensitive = true;
    report({ hasPassword: true });
  }
  return sensitive;
}

function hints(): Record<string, string | boolean> {
  const h: Record<string, string | boolean> = {};
  const host = location.hostname;
  if (/(^|\.)(stackoverflow|stackexchange|superuser|serverfault|askubuntu)\.com$/.test(host)) {
    h.soAccepted = !!document.querySelector('.accepted-answer, [itemprop="acceptedAnswer"]');
  }
  if (/(^|\.)youtube\.com$/.test(host)) {
    const channel = document.querySelector("ytd-video-owner-renderer ytd-channel-name a, #owner #channel-name a");
    if (channel?.textContent?.trim()) h.ytChannel = channel.textContent.trim().slice(0, 100);
  }
  return h;
}

function extract() {
  const clone = document.cloneNode(true) as Document;
  let text = "";
  try {
    text = new Readability(clone, { charThreshold: 300 }).parse()?.textContent ?? "";
  } catch {
    text = "";
  }
  if (!text) text = document.body?.innerText ?? "";
  text = text.replace(/\s+/g, " ").trim();
  const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
    .map((h) => (h.textContent ?? "").replace(/\s+/g, " ").trim())
    .filter((h) => h.length > 0)
    .slice(0, 20);
  const meta = document.querySelector<HTMLMetaElement>('meta[name="description"], meta[property="og:description"]');
  return {
    title: document.title.slice(0, 500),
    metaDescription: (meta?.content ?? "").slice(0, 500),
    headings,
    text: text.slice(0, MAX_TEXT),
    wordCount: text ? text.split(" ").length : 0,
    lang: document.documentElement.lang || null,
  };
}

function videoNow(): number {
  let total = videoSeconds;
  for (const v of Array.from(document.querySelectorAll("video"))) {
    const since = playingSince.get(v);
    if (since !== undefined) total += (performance.now() - since) / 1000;
  }
  return total;
}

function fullReport(): void {
  if (checkSensitive()) return;
  report({ extract: extract(), referrer: document.referrer || null, hints: hints(), scrollMax, videoS: videoNow() });
  dirty = false;
}

function scheduleExtract(delayMs: number): void {
  const run = () => fullReport();
  setTimeout(() => {
    const ric = (window as Window & { requestIdleCallback?: (cb: () => void, o?: object) => void }).requestIdleCallback;
    if (ric) ric(run, { timeout: 3000 });
    else run();
  }, delayMs);
}

// --- scroll and video ----------------------------------------------------------------------

let scrollTimer: number | undefined;
addEventListener(
  "scroll",
  () => {
    if (scrollTimer !== undefined) return;
    scrollTimer = window.setTimeout(() => {
      scrollTimer = undefined;
      const h = document.documentElement.scrollHeight;
      const pct = h > 0 ? Math.min(100, Math.round(((scrollY + innerHeight) / h) * 100)) : 0;
      if (pct > scrollMax) {
        scrollMax = pct;
        dirty = true;
      }
    }, 500);
  },
  { passive: true },
);

for (const type of ["play", "playing"]) {
  document.addEventListener(type, (e) => {
    if (!playingSince.has(e.target!)) playingSince.set(e.target!, performance.now());
  }, true);
}
for (const type of ["pause", "ended", "emptied"]) {
  document.addEventListener(type, (e) => {
    const since = playingSince.get(e.target!);
    if (since !== undefined) {
      videoSeconds += (performance.now() - since) / 1000;
      playingSince.delete(e.target!);
      dirty = true;
    }
  }, true);
}

// --- selection and copy --------------------------------------------------------------------

function inFormField(): boolean {
  const el = document.activeElement as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
}

function sendSelection(signal: "selection" | "copy"): void {
  if (checkSensitive() || inFormField()) return;
  const text = (getSelection()?.toString() ?? "").replace(/\s+/g, " ").trim();
  if (text.length < 20 || (signal === "selection" && text === lastSelection)) return;
  lastSelection = text;
  try {
    void chrome.runtime.sendMessage({ kind: "signal", signal, text: text.slice(0, 500) }).catch(() => undefined);
  } catch {
    // orphaned
  }
}

let selTimer: number | undefined;
document.addEventListener("mouseup", () => {
  clearTimeout(selTimer);
  selTimer = window.setTimeout(() => sendSelection("selection"), 800);
});
document.addEventListener("copy", () => sendSelection("copy"));

// --- lifecycle -----------------------------------------------------------------------------

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && dirty) fullReport();
});
addEventListener("pagehide", () => {
  if (dirty) fullReport();
});
setInterval(() => {
  checkSensitive();
  if (dirty && document.visibilityState === "visible") fullReport();
}, 15_000);

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.kind === "reextract") {
    scrollMax = 0;
    scheduleExtract(1500); // single-page app: give the new view time to render
  }
});

checkSensitive();
scheduleExtract(800);
