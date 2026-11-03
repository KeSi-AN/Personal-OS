from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from personal_os.browser import install
from personal_os.browser.enrich import classify, map_transition, search_query
from personal_os.config.schema import Config
from personal_os.core.clock import SystemClock
from personal_os.core.events import BrowserSignal, PageVisit
from personal_os.core.observer import MemoryStateStore
from personal_os.core.wiring import build_pipeline
from personal_os.observers.browser import Returns, ingest, parse_record
from personal_os.observers.spool import SpoolStats
from personal_os.storage import repo
from personal_os.storage.db import open_db

REPO = Path(__file__).resolve().parents[1]


def visit(**kw):
    rec = {
        "v": 1, "type": "visit", "id": "3f1c2a4e-1111-4222-8333-444455556666", "browser": "chrome",
        "start": "2026-11-03T13:00:00.000Z", "end": "2026-11-03T13:04:00.000Z",
        "last_seen": "2026-11-03T13:04:00.000Z", "url": "https://stackoverflow.com/questions/123/x",
        "title": "x - Stack Overflow", "tabId": 5, "windowId": 1, "transition": "link",
        "qualifiers": [], "referrer": None, "openerVisitId": None,
        "chainId": "3f1c2a4e-1111-4222-8333-444455556666", "activeMs": 180000, "scrollMax": 80,
        "videoS": 0, "bookmarked": False, "hasPassword": False, "incognito": False,
        "extract": {"title": "x", "metaDescription": "d", "headings": ["How to x"],
                    "text": "Use an httpx.Auth subclass. " * 10, "wordCount": 50, "lang": "en"},
        "hints": {"soAccepted": True},
    }  # fmt: skip
    rec.update(kw)
    return rec


# --- enrichment ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.google.com/search?q=httpx+retry&oq=x", ("google", "httpx retry")),
        ("https://www.google.co.in/search?q=sqlite+wal", ("google", "sqlite wal")),
        ("https://www.bing.com/search?q=a+b", ("bing", "a b")),
        ("https://duckduckgo.com/?q=rust+lifetimes", ("duckduckgo", "rust lifetimes")),
        ("https://search.brave.com/search?q=mv3", ("brave", "mv3")),
        ("https://www.youtube.com/results?search_query=asyncio", ("youtube", "asyncio")),
        ("https://github.com/search?q=watchdog&type=code", ("github", "watchdog")),
        ("https://www.google.com/maps", None),
        ("https://example.com/?q=x", None),
    ],
)
def test_search_query(url, expected):
    assert search_query(url) == expected


def test_transition_mapping():
    assert map_transition("link", ["forward_back"], None) == "back_forward"
    assert map_transition("generated", [], None) == "search"
    assert map_transition("link", [], "https://www.google.com/search?q=x") == "search"
    assert map_transition("typed", ["from_address_bar"], None) == "typed"
    assert map_transition("auto_bookmark", [], None) == "bookmark"


@pytest.mark.parametrize(
    ("url", "ctype", "subset"),
    [
        (
            "https://github.com/KeSi-AN/Personal-OS/pull/12",
            "github_pr",
            {"repo": "KeSi-AN/Personal-OS", "number": 12},
        ),
        ("https://github.com/a/b/issues/7", "github_issue", {"number": 7}),
        (
            "https://github.com/a/b/blob/main/src/x.py",
            "github_code",
            {"branch": "main", "file": "src/x.py"},
        ),
        ("https://github.com/a/b", "github_repo", {"repo": "a/b"}),
        ("https://github.com/settings/profile", "github", {}),
        (
            "https://docs.python.org/3.12/library/sqlite3.html",
            "docs",
            {"library": "python", "version": "3.12"},
        ),
        (
            "https://httpx.readthedocs.io/en/latest/advanced/",
            "docs",
            {"library": "httpx", "version": "latest"},
        ),
        ("https://docs.rs/tokio/1.40.0/tokio/", "docs", {"library": "tokio", "version": "1.40.0"}),
        ("https://developer.mozilla.org/en-US/docs/Web/API", "docs", {"library": "mdn"}),
        ("https://www.youtube.com/watch?v=abc123", "video", {"video_id": "abc123"}),
        ("https://mail.google.com/mail/u/0/#inbox", "mail", {}),
        ("https://meet.google.com/abc-defg-hij", "meeting", {}),
        ("https://claude.ai/chat/1", "chat", {}),
        ("https://www.google.com/search?q=x", "search_results", {"engine": "google"}),
        ("https://example.com/post", "other", {}),
    ],
)
def test_classify(url, ctype, subset):
    got_type, info = classify(url)
    assert got_type == ctype
    assert subset.items() <= info.items()


def test_classify_uses_hints_and_word_count():
    assert classify("https://stackoverflow.com/questions/1/x", hints={"soAccepted": False})[1] == {
        "question_id": 1, "accepted_answer": False,
    }  # fmt: skip
    assert (
        classify("https://www.youtube.com/watch?v=v", hints={"ytChannel": "Chan"})[1]["channel"]
        == "Chan"
    )
    assert classify("https://blog.example.com/p", word_count=800)[0] == "article"


# --- parsing -------------------------------------------------------------------------------


def test_parse_visit():
    events = parse_record(visit())
    assert events is not None and len(events) == 1
    e = events[0]
    p = e.payload
    assert isinstance(p, PageVisit)
    assert str(e.id) == "3f1c2a4e-1111-4222-8333-444455556666"
    assert (e.ts_end - e.ts_start).total_seconds() == 240  # type: ignore[operator]
    assert p.content_type == "stackoverflow" and p.enrichment["accepted_answer"] is True
    assert p.active_dwell_ms == 180000 and p.scroll_max_pct == 80
    assert (
        p.extract is not None
        and p.extract.headings == ["How to x"]
        and len(p.extract.content_hash) == 32
    )


def test_open_visit_and_search_page():
    rec = visit(end=None, url="https://www.google.com/search?q=sqlite+wal+mode", extract=None)
    events = parse_record(rec)
    assert events is not None and len(events) == 2
    page, search = events
    assert page.ts_end is None and page.payload.search_query == "sqlite wal mode"  # type: ignore[union-attr]
    assert isinstance(search.payload, BrowserSignal) and search.payload.text == "sqlite wal mode"
    again = parse_record(rec)
    assert again is not None and again[1].id == search.id  # re-sends upsert the same search event


def test_signals_and_malformed():
    sel = parse_record({"type": "signal", "kind": "copy", "url": "https://a.dev/x",
                        "text": "copied text", "at": "2026-11-03T13:00:00Z"})  # fmt: skip
    assert sel and sel[0].kind == "browser.copy"
    assert (
        parse_record({"type": "signal", "kind": "keystroke", "at": "2026-11-03T13:00:00Z"}) is None
    )
    assert parse_record({"type": "nope"}) is None
    assert parse_record(visit(id="not-a-uuid")) is None


def test_returns_counted_once_per_visit_and_hashed():
    store = MemoryStateStore()
    r = Returns(store)
    url = "https://docs.python.org/3/library/asyncio.html"
    assert r.count_and_record(url, "v1", 100) == 0
    assert r.count_and_record(url, "v1", 100) == 0  # re-send of the same visit
    assert r.count_and_record(url, "v2", 101) == 1
    assert r.count_and_record(url, "v3", 200) == 0  # older than 7 days
    r.save()
    assert url not in json.dumps(store.get("browser.returns"))


# --- end to end: spool → filter → DB --------------------------------------------------------


def test_spool_to_database_respects_privacy(tmp_path: Path, config: Config):
    spool = tmp_path / "spool"
    spool.mkdir()
    lines = [
        visit(),
        visit(id="3f1c2a4e-1111-4222-8333-000000000001", url="https://web.whatsapp.com/", title="WhatsApp"),
        visit(id="3f1c2a4e-1111-4222-8333-000000000002", url="https://bank.example/login", hasPassword=True),
        visit(id="3f1c2a4e-1111-4222-8333-000000000003", url="https://mail.google.com/mail/u/0/#inbox/1",
              title="Re: salary - Gmail"),
        visit(id="3f1c2a4e-1111-4222-8333-000000000004", url="https://example.com/x", activeMs=1500),
        visit(id="3f1c2a4e-1111-4222-8333-000000000005", url="https://example.com/y?token=abc123&utm_source=n"),
    ]  # fmt: skip
    (spool / "chrome-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", "utf-8")
    events = ingest(spool, SpoolStats(), Returns(None))
    conn = open_db(tmp_path / "b.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    pipe.process(events)
    stored = {e.payload.url: e for e in repo.iter_events(conn, source="browser")}  # type: ignore[union-attr]
    assert "https://stackoverflow.com/questions/123/x" in stored
    assert not any("whatsapp" in u for u in stored)  # blocked domain
    assert not any("bank.example" in u for u in stored)  # password field
    assert not any(u.endswith("/x") and "example.com" in u for u in stored)  # 1.5 s bounce
    assert (
        "https://mail.google.com/" in stored
        and stored["https://mail.google.com/"].privacy_class == "sensitive"
    )
    assert (
        "https://example.com/y?token=[redacted]" in stored
    )  # secret param redacted, tracking stripped
    assert repo.search(conn, "httpx Auth subclass")  # page text searchable
    c = pipe.privacy.counters.blocked
    assert c["blocked_domain"] == 1 and c["password_field"] == 1 and c["short_dwell"] == 1


# --- native host ---------------------------------------------------------------------------


def _frame(msg: object) -> bytes:
    data = json.dumps(msg).encode()
    return struct.pack("<I", len(data)) + data


def _replies(raw: bytes) -> list[dict]:
    out, i = [], 0
    while i + 4 <= len(raw):
        (n,) = struct.unpack("<I", raw[i : i + 4])
        out.append(json.loads(raw[i + 4 : i + 4 + n]))
        i += 4 + n
    return out


def run_host(home: Path, *messages: object, extra: bytes = b"") -> list[dict]:
    stdin = b"".join(_frame(m) for m in messages) + extra
    out = subprocess.run([sys.executable, "-m", "personal_os.browser.native_host", f"--home={home}",
                          "--browser=chrome"], input=stdin, capture_output=True, timeout=60)  # fmt: skip
    assert out.returncode == 0, out.stderr
    return _replies(out.stdout)


def test_native_host_protocol(tmp_path: Path):
    replies = run_host(
        tmp_path,
        {"type": "hello", "version": "1.0.0"},
        visit(),
        {
            "type": "signal",
            "kind": "copy",
            "text": "x",
            "incognito": True,
            "at": "2026-11-03T13:00:00Z",
        },
        {"type": "exec", "cmd": "calc.exe"},
        extra=b"\x05\x00\x00\x00{bad}",
    )
    assert replies[0]["ok"] and replies[0]["paused"] is False and replies[0]["host_version"]
    assert replies[1] == {"ok": True, "paused": False}
    assert replies[2]["dropped"] is True  # incognito never written
    assert replies[3]["ok"] is False  # unknown types refused
    assert replies[4]["ok"] is False  # malformed JSON refused, host keeps going
    lines = [json.loads(x) for f in (tmp_path / "spool" / "browser").glob("*.jsonl")
             for x in f.read_text("utf-8").splitlines()]  # fmt: skip
    assert [r["type"] for r in lines] == ["visit"]


def test_native_host_respects_pause(tmp_path: Path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "paused").write_text("")
    replies = run_host(tmp_path, {"type": "hello"}, visit())
    assert replies[0]["paused"] is True and replies[1]["dropped"] is True
    assert not (tmp_path / "spool" / "browser").exists()


def test_native_host_refuses_oversized_messages(tmp_path: Path):
    big = {"type": "visit", "pad": "x" * 1_100_000}
    replies = run_host(tmp_path, big, {"type": "hello"})
    assert replies[0]["ok"] is False and replies[1]["ok"] is True


# --- installer -----------------------------------------------------------------------------


def test_extension_id_matches_manifest_key():
    manifest = json.loads((REPO / "extensions" / "browser" / "manifest.json").read_text("utf-8"))
    assert install.extension_id_from_key(manifest["key"]) == install.EXTENSION_ID
    assert manifest["incognito"] == "not_allowed"
    assert manifest["manifest_version"] == 3


def test_host_files_pin_origin_and_isolate_python(tmp_path: Path):
    manifest = install.write_host_files(tmp_path, "chrome", home=tmp_path / "home")
    data = json.loads(manifest.read_text("utf-8"))
    assert data["name"] == "com.personalos.host" and data["type"] == "stdio"
    assert data["allowed_origins"] == [f"chrome-extension://{install.EXTENSION_ID}/"]
    bat = Path(data["path"]).read_text("utf-8")
    assert " -I -m personal_os.browser.native_host --browser=chrome" in bat
    assert f'--home="{tmp_path / "home"}"' in bat


def test_titles_search_urls_and_documents_are_cleaned(config: Config):
    events = parse_record(visit(url="https://www.google.com/search?q=sqlite+wal&oq=sq&gs_lcrp=EgZjaHJ&sourceid=chrome&ie=UTF-8",
                                title="(3995) sqlite wal - Google Search"))  # fmt: skip
    assert events is not None
    p = events[0].payload
    assert p.url == "https://www.google.com/search?q=sqlite+wal"  # type: ignore[union-attr]
    assert p.title == "sqlite wal - Google Search"  # type: ignore[union-attr]

    from personal_os.privacy.filter import PrivacyFilter
    from personal_os.privacy.rules import PrivacyRules

    f = PrivacyFilter(PrivacyRules.from_config(config.privacy))
    doc = parse_record(visit(url="https://docs.google.com/document/d/abc/edit", title="Plan - Google Docs",
                             extract={"text": "my private plan; me@example.com", "title": "Plan"}))  # fmt: skip
    assert doc is not None and doc[0].payload.content_type == "document"  # type: ignore[union-attr]
    stored = f.apply(doc[0])
    assert stored is not None and stored.payload.extract is None  # type: ignore[union-attr]
    assert stored.payload.title == "Plan - Google Docs"  # type: ignore[union-attr]

    other = f.apply(
        parse_record(visit(extract={"text": "write to addya.someone+x@gmail.com today"}))[0]
    )  # type: ignore[index]
    assert other is not None and "gmail.com" not in other.payload.extract.main_text  # type: ignore[union-attr]
