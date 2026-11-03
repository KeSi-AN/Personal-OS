import assert from "node:assert/strict";
import { test } from "node:test";

import { type FocusState, activeMsAt, isTrackable, newVisit, pushBounded, syncActive, toRecord } from "../src/visit";

function visit(tabId = 1, id = "v1") {
  return newVisit({ id, tabId, windowId: 1, url: "https://a.dev/x", title: "A", now: 0,
                    transition: "link", qualifiers: [], previous: null, opener: null });
}

const focused = (tab: number | null): FocusState => ({ activeTab: tab, windowFocused: true, idle: "active" });

test("dwell counts only while active tab + focused window + not idle", () => {
  const v = visit();
  syncActive([v], focused(1), 1_000); // becomes active at 1 s
  syncActive([v], { ...focused(1), windowFocused: false }, 5_000); // browser loses focus: +4 s
  syncActive([v], focused(1), 10_000); // focus back
  syncActive([v], { ...focused(1), idle: "idle" }, 12_000); // idle: +2 s
  syncActive([v], focused(2), 20_000); // another tab active
  assert.equal(activeMsAt(v, 30_000), 6_000);
});

test("open visit reports dwell so far without stopping the clock", () => {
  const v = visit();
  syncActive([v], focused(1), 0);
  assert.equal(toRecord(v, 3_000, null, "chrome").activeMs, 3_000);
  assert.equal(v.activeSince, 0);
  assert.equal(toRecord(v, 3_000, null, "chrome").end, null);
});

test("chains follow links and openers, typed URLs start a new chain", () => {
  const a = visit(1, "a");
  const b = newVisit({ id: "b", tabId: 1, windowId: 1, url: "https://a.dev/y", title: "", now: 1,
                       transition: "link", qualifiers: [], previous: a, opener: null });
  const c = newVisit({ id: "c", tabId: 2, windowId: 1, url: "https://b.dev", title: "", now: 2,
                       transition: "link", qualifiers: [], previous: null, opener: b });
  const d = newVisit({ id: "d", tabId: 1, windowId: 1, url: "https://c.dev", title: "", now: 3,
                       transition: "typed", qualifiers: [], previous: b, opener: null });
  assert.equal(b.chainId, "a");
  assert.equal(c.chainId, "a");
  assert.equal(c.openerVisitId, "b");
  assert.equal(d.chainId, "d");
});

test("pages with a password field never send text", () => {
  const v = visit();
  v.extract = { title: "t", metaDescription: "", headings: [], text: "secret page", wordCount: 2, lang: null };
  v.hints = { soAccepted: true };
  v.hasPassword = true;
  const r = toRecord(v, 1, 1, "chrome");
  assert.equal(r.extract, null);
  assert.deepEqual(r.hints, {});
  assert.equal(r.hasPassword, true);
});

test("only http(s) pages are tracked; outbox is bounded", () => {
  assert.ok(isTrackable("https://x.dev"));
  for (const u of ["chrome://settings", "file:///c:/x", "about:blank", undefined]) assert.ok(!isTrackable(u));
  let q: number[] = [];
  for (let i = 0; i < 5; i++) q = pushBounded(q, i, 3);
  assert.deepEqual(q, [2, 3, 4]);
});
