"""The privacy filter: the single stage every event passes before storage (DESIGN.md §4.3).

It fails closed: if anything inside raises, the event is dropped and counted, never stored.
It never lowers an event's privacy class.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from personal_os.core.events import (
    BrowserSignal,
    ClipboardText,
    Event,
    FsBulkChange,
    FsChange,
    PageExtract,
    PageVisit,
    PrivacyClass,
    Redactable,
    RedactContext,
    Source,
    WindowFocus,
    stricter,
)
from personal_os.core.logging import event_ref, get_logger
from personal_os.privacy.redact import REDACTED, TextRedactor, origin_only
from personal_os.privacy.rules import NON_WEB_SCHEMES, PrivacyRules, domain_of, scheme_of

R = TypeVar("R", bound=Redactable)

_log = get_logger("privacy")


@dataclass
class FilterCounters:
    passed: int = 0
    paused: int = 0
    blocked: Counter[str] = field(default_factory=Counter)  # by reason
    sensitive: Counter[str] = field(default_factory=Counter)  # by source
    errors: Counter[str] = field(default_factory=Counter)  # by source
    redactions: Counter[str] = field(default_factory=Counter)  # by detector


class PrivacyFilter:
    def __init__(
        self,
        rules: PrivacyRules,
        redactor: TextRedactor | None = None,
        *,
        is_paused: Callable[[Event], bool] = lambda _event: False,
    ) -> None:
        self.rules = rules
        self.redactor = redactor or TextRedactor()
        self.is_paused = is_paused
        self.counters = FilterCounters()

    def apply_many(self, events: Iterable[Event]) -> list[Event]:
        return [e for e in (self.apply(ev) for ev in events) if e is not None]

    def apply(self, event: Event) -> Event | None:
        try:
            result = self._apply(event)
        except Exception as exc:
            self.counters.errors[event.source] += 1
            _log.error(
                "privacy filter error; event dropped",
                extra={"fields": {**event_ref(event), "exc_type": type(exc).__name__}},
            )
            return None
        if result is not None:
            self.counters.passed += 1
            if result.privacy_class == PrivacyClass.SENSITIVE:
                self.counters.sensitive[result.source] += 1
        return result

    # --- stages ----------------------------------------------------------------------------

    def _apply(self, event: Event) -> Event | None:
        if event.source != Source.META and self.is_paused(event):
            self.counters.paused += 1
            return None
        if event.privacy_class == PrivacyClass.BLOCKED:
            self._block("observer_marked_blocked")
            return None
        reason = self._block_reason(event)
        if reason:
            self._block(reason)
            return None

        cls: PrivacyClass = event.privacy_class
        payload = event.payload

        if isinstance(payload, WindowFocus) and self.rules.title_needs_redaction(payload.title):
            payload = payload.model_copy(update={"title": REDACTED})
            self.counters.redactions["title_rule"] += 1

        if isinstance(payload, FsChange) and (
            payload.sensitive
            or self.rules.is_secret_path(payload.path)
            or self.rules.is_secret_path(payload.dest_path)
        ):
            payload = payload.model_copy(
                update={
                    "sensitive": True,
                    "size": None,
                    "lines_added": None,
                    "lines_removed": None,
                    "symbols_added": [],
                    "symbols_removed": [],
                    "symbols_changed": [],
                }
            )
            cls = stricter(cls, PrivacyClass.SENSITIVE)

        if isinstance(payload, PageVisit | BrowserSignal) and self.rules.is_sensitive_domain(
            _domain(payload)
        ):
            cls = stricter(cls, PrivacyClass.SENSITIVE)

        payload = self._redact(payload)

        if cls == PrivacyClass.NORMAL and self._has_sensitive_keyword(payload):
            cls = PrivacyClass.SENSITIVE
        if cls == PrivacyClass.SENSITIVE:
            payload = self._strip(payload)

        return event.model_copy(update={"payload": payload, "privacy_class": cls})

    def _block(self, reason: str) -> None:
        self.counters.blocked[reason] += 1
        return None

    def _block_reason(self, event: Event) -> str | None:
        p = event.payload
        rules = self.rules
        if isinstance(p, WindowFocus) and rules.is_blocked_app(p.exe, p.app):
            return "blocked_app"
        if isinstance(p, ClipboardText) and rules.is_blocked_app(p.app):
            return "blocked_app"
        if isinstance(p, PageVisit | BrowserSignal):
            if p.incognito:
                return "incognito"
            if isinstance(p, PageVisit) and p.has_password_field:
                return "password_field"
            if p.url and scheme_of(p.url) in NON_WEB_SCHEMES:
                return "non_web_scheme"
            if rules.is_blocked_domain(_domain(p)):
                return "blocked_domain"
            if (
                isinstance(p, PageVisit)
                and event.ts_end is not None
                and p.active_dwell_ms < rules.min_dwell_ms
                and not (p.bookmarked or p.searched or p.returned_count_7d > 0)
            ):
                return "short_dwell"
        if isinstance(p, FsChange) and (
            rules.is_protected_path(p.path) or rules.is_protected_path(p.dest_path)
        ):
            return "protected_path"
        if isinstance(p, FsBulkChange) and rules.is_protected_path(p.dir):
            return "protected_path"
        return None

    # --- generic field handling ------------------------------------------------------------

    def _redact(self, model: R) -> R:
        ctx: RedactContext = model.REDACT_CONTEXT
        updates: dict[str, Any] = {}
        for name in model.TEXT_FIELDS:
            value = getattr(model, name)
            new = self._redact_value(value, ctx)
            if new != value:
                updates[name] = new
        for name in model.URL_FIELDS:
            value = getattr(model, name)
            if isinstance(value, str) and value:
                new_url = self.redactor.redact_url(value, self.counters.redactions)
                if new_url != value:
                    updates[name] = new_url
        for name in type(model).model_fields:
            value = getattr(model, name)
            if isinstance(value, Redactable):
                new_model = self._redact(value)
                if new_model is not value:
                    updates[name] = new_model
            elif isinstance(value, list) and value and isinstance(value[0], Redactable):
                updates[name] = [self._redact(v) for v in value]
            elif isinstance(value, dict) and value:
                cleaned = {k: self._redact_value(v, ctx) for k, v in value.items()}
                if cleaned != value:
                    updates[name] = cleaned
        return model.model_copy(update=updates) if updates else model

    def _redact_value(self, value: Any, ctx: RedactContext) -> Any:
        if isinstance(value, str):
            text, hits = self.redactor.redact(value, ctx)
            self.counters.redactions.update(hits)
            return text
        if isinstance(value, list):
            return [self._redact_value(v, ctx) for v in value]
        return value

    def _has_sensitive_keyword(self, model: Redactable) -> bool:
        """Soft rule on titles only (window and page titles). Commands and commit messages that
        merely mention "password" are handled by redaction, not by hiding the whole event."""
        pattern = self.rules.sensitive_keyword_re
        if pattern is None:
            return False
        titles = [getattr(model, "title", None)]
        extract = getattr(model, "extract", None)
        if isinstance(extract, PageExtract):
            titles.append(extract.title)
        return any(isinstance(t, str) and pattern.search(t) for t in titles)

    def _strip(self, model: R) -> R:
        """Remove all free text from a sensitive event; URLs keep only their origin."""
        updates: dict[str, Any] = {}
        for name in model.TEXT_FIELDS:
            value = getattr(model, name)
            if isinstance(value, str) and value:
                updates[name] = REDACTED
            elif isinstance(value, list):
                updates[name] = []
        for name in model.URL_FIELDS:
            value = getattr(model, name)
            if isinstance(value, str) and value:
                updates[name] = origin_only(value)
        for name in type(model).model_fields:
            value = getattr(model, name)
            if isinstance(value, PageExtract):
                updates[name] = None
            elif isinstance(value, Redactable):
                updates[name] = self._strip(value)
            elif isinstance(value, dict) and value:
                updates[name] = {}
        return model.model_copy(update=updates) if updates else model


def _domain(p: PageVisit | BrowserSignal) -> str:
    # Never trust a domain supplied by the client over the one in the URL.
    return domain_of(p.url) or p.domain.lower()
