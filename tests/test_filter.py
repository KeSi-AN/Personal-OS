from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from personal_os.core.events import (
    BrowserSignal,
    ClipboardText,
    Event,
    FsBulkChange,
    FsChange,
    GitCommit,
    MetaEvent,
    PageExtract,
    PageVisit,
    PrivacyClass,
    TerminalCommand,
    WindowFocus,
)
from personal_os.privacy.filter import PrivacyFilter
from personal_os.privacy.redact import REDACTED
from tests.conftest import T0


def ev(payload, *, dwell_s: float | None = None, **kw) -> Event:
    if dwell_s is None:
        return Event.point(payload, T0, observer_version="t", **kw)
    return Event.interval(payload, T0, T0 + timedelta(seconds=dwell_s), observer_version="t", **kw)


def visit(url: str, **kw) -> PageVisit:
    kw.setdefault("active_dwell_ms", 60_000)
    return PageVisit(url=url, **kw)


def test_normal_event_passes_with_redaction(privacy: PrivacyFilter):
    out = privacy.apply(ev(TerminalCommand(command="deploy --token abc123secret", shell="bash")))
    assert out is not None and out.privacy_class == PrivacyClass.NORMAL
    assert out.payload.command == f"deploy --token {REDACTED}"  # type: ignore[union-attr]
    assert privacy.counters.redactions["secret_flag"] == 1


def test_blocked_app_dropped_and_counted(privacy: PrivacyFilter):
    assert privacy.apply(ev(WindowFocus(app="WhatsApp", exe="WhatsApp.exe", title="Chat"))) is None
    assert privacy.apply(ev(WindowFocus(app="KeePassXC", exe="keepassxc.exe"))) is None
    assert privacy.apply(ev(ClipboardText(text="x", app="1Password.exe"))) is None
    assert privacy.counters.blocked["blocked_app"] == 3


def test_title_rule_redacts_but_keeps_app(privacy: PrivacyFilter):
    out = privacy.apply(ev(WindowFocus(app="Brave", exe="brave.exe", title="(2) WhatsApp - Brave")))
    assert out is not None
    assert out.payload.title == REDACTED and out.payload.app == "Brave"  # type: ignore[union-attr]
    out = privacy.apply(ev(WindowFocus(app="Brave", title="Secret thing - Private window - Brave")))
    assert out is not None and out.payload.title == REDACTED  # type: ignore[union-attr]


def test_messaging_domains_blocked_including_subdomains(privacy: PrivacyFilter):
    for url in (
        "https://web.whatsapp.com/",
        "https://www.instagram.com/reels/",
        "https://instagram.com",
    ):
        assert privacy.apply(ev(visit(url), dwell_s=60)) is None
    assert privacy.counters.blocked["blocked_domain"] == 3
    # a look-alike that merely contains the name is not blocked
    assert privacy.apply(ev(visit("https://notinstagram.dev/x"), dwell_s=60)) is not None


def test_bank_keyword_domains_blocked(privacy: PrivacyFilter):
    assert privacy.apply(ev(visit("https://netbanking.hdfcbank.com/login"), dwell_s=60)) is None
    assert privacy.apply(ev(visit("https://www.paypal.com/"), dwell_s=60)) is None


def test_domain_comes_from_url_not_client_field(privacy: PrivacyFilter):
    sneaky = visit("https://web.whatsapp.com/", domain="example.com")
    assert privacy.apply(ev(sneaky, dwell_s=60)) is None


def test_incognito_password_and_non_web_dropped(privacy: PrivacyFilter):
    assert privacy.apply(ev(visit("https://a.dev", incognito=True), dwell_s=60)) is None
    assert privacy.apply(ev(visit("https://a.dev", has_password_field=True), dwell_s=60)) is None
    assert privacy.apply(ev(visit("chrome://settings"), dwell_s=60)) is None
    assert privacy.apply(ev(visit("file:///C:/secret.pdf"), dwell_s=60)) is None
    signal = BrowserSignal(kind="browser.selection", url="https://a.dev", text="hi", incognito=True)
    assert privacy.apply(ev(signal)) is None
    assert privacy.counters.blocked["incognito"] == 2


def test_short_dwell_dropped_unless_searched_or_bookmarked(privacy: PrivacyFilter):
    assert privacy.apply(ev(visit("https://a.dev", active_dwell_ms=2000), dwell_s=2)) is None
    assert privacy.apply(ev(visit("https://a.dev", active_dwell_ms=2000, searched=True), dwell_s=2))
    assert privacy.apply(
        ev(visit("https://a.dev", active_dwell_ms=2000, bookmarked=True), dwell_s=2)
    )
    # still-open visits are not judged yet
    assert privacy.apply(Event.interval(visit("https://a.dev", active_dwell_ms=0), T0,
                                        observer_version="t"))  # fmt: skip


def test_allowlist_mode(config):
    from personal_os.privacy.rules import PrivacyRules

    cfg = config.privacy.model_copy(
        update={"allowlist_mode": True, "allowed_domains": ["docs.python.org"]}
    )
    f = PrivacyFilter(PrivacyRules.from_config(cfg))
    assert f.apply(ev(visit("https://docs.python.org/3/"), dwell_s=60)) is not None
    assert f.apply(ev(visit("https://example.com/"), dwell_s=60)) is None


def test_sensitive_domain_keeps_only_origin_and_time(privacy: PrivacyFilter):
    payload = visit(
        "https://mail.google.com/mail/u/0/#inbox/abc",
        title="Re: salary - Gmail",
        extract=PageExtract(main_text="body text"),
        search_query="from:boss",
    )
    out = privacy.apply(ev(payload, dwell_s=120))
    assert out is not None and out.privacy_class == PrivacyClass.SENSITIVE
    p = out.payload
    assert isinstance(p, PageVisit)
    assert p.url == "https://mail.google.com/"
    assert p.title == REDACTED and p.search_query == REDACTED and p.extract is None
    assert p.active_dwell_ms == 60_000


def test_sensitive_keyword_in_title(privacy: PrivacyFilter):
    out = privacy.apply(ev(WindowFocus(app="Chrome", title="Reset your password - Chrome")))
    assert out is not None and out.privacy_class == PrivacyClass.SENSITIVE
    assert out.payload.title == REDACTED  # type: ignore[union-attr]
    # commands that mention a password are redacted, not hidden
    out = privacy.apply(ev(TerminalCommand(command="echo password reset done")))
    assert out is not None and out.privacy_class == PrivacyClass.NORMAL


def test_secret_files_never_carry_details(privacy: PrivacyFilter):
    for path in ("C:/p/.env", "C:/p/.env.local", "C:/Users/a/.ssh/config", "C:/k/server.PEM",
                 "C:\\Users\\a\\.aws\\credentials", "C:/p/id_ed25519.pub"):  # fmt: skip
        out = privacy.apply(ev(FsChange(path=path, change="modified", size=10, lines_added=3,
                                        symbols_added=["x"])))  # fmt: skip
        assert out is not None, path
        p = out.payload
        assert isinstance(p, FsChange)
        assert out.privacy_class == PrivacyClass.SENSITIVE and p.sensitive, path
        assert p.size is None and p.lines_added is None and p.symbols_added == [], path
    out = privacy.apply(ev(FsChange(path="C:/p/environment.py", change="modified")))
    assert out is not None and out.privacy_class == PrivacyClass.NORMAL


def test_own_data_dir_is_never_observed(privacy: PrivacyFilter, config):
    inside = str(config.data_dir / "personal_os.db")
    assert privacy.apply(ev(FsChange(path=inside, change="modified"))) is None
    assert privacy.apply(ev(FsBulkChange(dir=str(config.data_dir).upper(), count=5))) is None


def test_git_context_keeps_commit_hashes(privacy: PrivacyFilter):
    sha = "3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a"
    out = privacy.apply(ev(GitCommit(repo_root="C:/r", sha=sha, message=f"Revert {sha}")))
    assert out is not None and out.payload.message == f"Revert {sha}"  # type: ignore[union-attr]


def test_nested_extract_is_redacted(privacy: PrivacyFilter):
    payload = visit(
        "https://blog.dev/post",
        extract=PageExtract(
            main_text="use key AKIAIOSFODNN7EXAMPLE", headings=["--password hunter2"]
        ),
    )
    out = privacy.apply(ev(payload, dwell_s=60))
    assert out is not None
    extract = out.payload.extract  # type: ignore[union-attr]
    assert "AKIAIOSFODNN7EXAMPLE" not in extract.main_text
    assert "hunter2" not in extract.headings[0]


def test_pause_drops_everything_but_meta(privacy: PrivacyFilter):
    privacy.is_paused = lambda _e: True
    assert privacy.apply(ev(WindowFocus(app="Code"))) is None
    assert privacy.apply(ev(MetaEvent(kind="meta.resume"))) is not None
    assert privacy.counters.paused == 1


def test_fails_closed(privacy: PrivacyFilter):
    class Broken:
        name = "broken"

        def redact(self, text, context):
            raise RuntimeError("bug")

    privacy.redactor.detectors.append(Broken())  # type: ignore[arg-type]
    assert privacy.apply(ev(TerminalCommand(command="ls"))) is None
    assert privacy.counters.errors["terminal"] == 1


_CLASSES = st.sampled_from([PrivacyClass.NORMAL, PrivacyClass.SENSITIVE, PrivacyClass.BLOCKED])


@settings(  # the filter's counters accumulate across examples; that doesn't affect the property
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(cls=_CLASSES, title=st.text(max_size=80), command=st.text(max_size=80))
def test_property_privacy_class_never_decreases(privacy: PrivacyFilter, cls, title, command):
    rank = {PrivacyClass.NORMAL: 0, PrivacyClass.SENSITIVE: 1, PrivacyClass.BLOCKED: 2}
    for payload in (WindowFocus(app="Code", title=title), TerminalCommand(command=command)):
        out = privacy.apply(ev(payload, privacy_class=cls))
        if cls == PrivacyClass.BLOCKED:
            assert out is None
        elif out is not None:
            assert rank[out.privacy_class] >= rank[cls]
