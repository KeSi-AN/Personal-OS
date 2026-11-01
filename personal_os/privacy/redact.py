"""Secret redaction for free text and URLs (DESIGN.md §4.3 step 4).

Two kinds of detector:
  * pattern detectors for known secret shapes (tokens, keys, headers, flags, assignments);
  * an entropy detector for random-looking tokens that match no known shape.

Detectors are pluggable: anything with `name` and `redact(text, context) -> (text, hits)`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from personal_os.core.events import RedactContext

REDACTED = "[redacted]"


class Redactor(Protocol):
    @property
    def name(self) -> str: ...

    def redact(self, text: str, context: RedactContext) -> tuple[str, int]: ...


@dataclass(frozen=True)
class PatternRedactor:
    """Replaces each match with [redacted], keeping the optional `keep` and `tail` groups."""

    name: str
    pattern: re.Pattern[str]

    def redact(self, text: str, context: RedactContext) -> tuple[str, int]:
        hits = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal hits
            hits += 1
            groups = m.groupdict()
            return (groups.get("keep") or "") + REDACTED + (groups.get("tail") or "")

        return self.pattern.sub(repl, text), hits


def _p(name: str, regex: str, flags: int = 0) -> PatternRedactor:
    return PatternRedactor(name, re.compile(regex, flags))


_NOT_ALREADY = r"(?!\[redacted\])"
_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;&|]+)"
_AUTH_SCHEMES = r"(?:bearer|basic|token|digest|negotiate)"
_SECRET_WORDS = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|credentials?|auth[_-]?token)"
)

DEFAULT_PATTERNS: tuple[PatternRedactor, ...] = (
    _p(
        "pem_private_key",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    ),
    _p("aws_access_key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    _p("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"),
    _p("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    _p("slack_token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    _p("stripe_key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    _p("llm_api_key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    _p("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    _p("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    _p(
        "auth_header",
        # Either consume the scheme word, or assert there is none: the scheme itself is never
        # taken as the secret (keeps a second pass over redacted text a no-op).
        r"(?P<keep>\b(?:proxy-)?authorization\s*[:=]\s*"
        rf"(?:{_AUTH_SCHEMES}\s+|(?!{_AUTH_SCHEMES}\b)))" + _NOT_ALREADY + r"[^\s\"',;]+",
        re.IGNORECASE,
    ),
    _p("bearer", r"(?P<keep>\bbearer\s+)" + _NOT_ALREADY + r"[A-Za-z0-9._~+/\-]{8,}=*", re.I),
    _p("url_userinfo", r"(?P<keep>\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+(?P<tail>@)", re.I),
    _p(
        "secret_flag",
        r"(?P<keep>(?<![\w-])--?(?:password|passwd|pwd|pass|token|secret|api[-_]?key"
        r"|access[-_]?key|client[-_]?secret|auth[-_]?token)(?:=|\s+))" + _NOT_ALREADY + _VALUE,
        re.IGNORECASE,
    ),
    _p(
        "mysql_inline_password",
        r"(?P<keep>\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n|;&]*?\s-p)(?=\S)"
        + _NOT_ALREADY
        + r"[^\s\"']+",
    ),
    _p(
        "secret_assignment",
        r"(?P<keep>\b[A-Za-z0-9_.\-]*"
        + _SECRET_WORDS
        + r"[A-Za-z0-9_\-]*\s*(?::=|=|:)\s*)"
        + _NOT_ALREADY
        + r"(?=[^\s])"
        + _VALUE,
        re.IGNORECASE,
    ),
)


# --- entropy -------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+=_\-])[A-Za-z0-9+=_\-]{20,}(?![A-Za-z0-9+=_\-])")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def looks_random(token: str, context: RedactContext) -> bool:
    if _UUID_RE.match(token):
        return False
    if _HEX_RE.match(token):
        if len(token) < 32:
            return False
        if len(token) in (40, 64) and context in ("git", "url"):
            return False  # commit / object hashes
        return shannon_entropy(token) > 3.0
    if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
        return False  # plain words and identifiers without digits
    # A random string of length n has entropy a little under log2(n); scale the bar with length.
    # Tuned on 2,000 random tokens (99% recall) vs. common identifiers and version strings.
    threshold = min(4.4, math.log2(len(token)) - 0.7)
    return shannon_entropy(token) > threshold


@dataclass(frozen=True)
class EntropyRedactor:
    name: str = "high_entropy"

    def redact(self, text: str, context: RedactContext) -> tuple[str, int]:
        hits = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal hits
            if looks_random(m.group(0), context):
                hits += 1
                return REDACTED
            return m.group(0)

        return _TOKEN_RE.sub(repl, text), hits


# --- URLs ----------------------------------------------------------------------------------

TRACKING_PARAMS = frozenset(
    {
        "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "mc_eid", "mc_cid",
        "ref_src", "igshid", "_hsenc", "_hsmi", "mkt_tok", "vero_id", "spm", "si", "_ga", "_gl",
    }
)  # fmt: skip
_SECRET_PARAM_RE = re.compile(
    r"(?i)^(?:.*(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|sig|signature"
    r"|credential|session|auth).*|code|state|key|otp|nonce)$"
)
_URL_IN_TEXT_RE = re.compile(r"\bhttps?://[^\s\"'<>]+", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443}


class TextRedactor:
    """Runs all detectors over free text. Returns the redacted text and hits per detector."""

    def __init__(
        self,
        patterns: Sequence[Redactor] = DEFAULT_PATTERNS,
        *,
        entropy: bool = True,
        extra: Iterable[Redactor] = (),
    ) -> None:
        self.detectors: list[Redactor] = [*patterns, *extra]
        if entropy:
            self.detectors.append(EntropyRedactor())

    def redact(self, text: str, context: RedactContext = "text") -> tuple[str, Counter[str]]:
        hits: Counter[str] = Counter()
        if not text:
            return text, hits
        if context == "command" and text.lstrip().startswith("git "):
            context = "git"
        if context != "url":
            text = _URL_IN_TEXT_RE.sub(lambda m: self.redact_url(m.group(0), hits), text)
        for detector in self.detectors:
            text, n = detector.redact(text, context)
            if n:
                hits[detector.name] += n
        return text, hits

    def redact_url(self, url: str, hits: Counter[str] | None = None) -> str:
        """Strip userinfo and tracking params, redact secret-looking params and fragments."""
        hits = hits if hits is not None else Counter()
        try:
            parts = urlsplit(url.strip())
            host = parts.hostname
            port = parts.port
        except ValueError:
            hits["bad_url"] += 1
            return REDACTED
        if not parts.scheme or not host:
            text, h = self.redact(url, "text")
            hits.update(h)
            return text
        if parts.username or parts.password:
            hits["url_userinfo"] += 1
        netloc = host.lower()
        if ":" in netloc:
            netloc = f"[{netloc}]"  # IPv6
        if port and _DEFAULT_PORTS.get(parts.scheme.lower()) != port:
            netloc += f":{port}"

        query: list[tuple[str, str]] = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lk = key.lower()
            if lk.startswith("utm_") or lk in TRACKING_PARAMS:
                hits["tracking_param"] += 1
                continue
            if _SECRET_PARAM_RE.match(key) and value:
                hits["secret_param"] += 1
                query.append((key, REDACTED))
                continue
            value, h = self._redact_fragment(value)
            hits.update(h)
            query.append((key, value))

        fragment = parts.fragment
        if fragment and ("=" in fragment or len(fragment) > 64):
            hits["fragment"] += 1
            fragment = ""
        path, h = self._redact_fragment(parts.path)
        hits.update(h)
        return urlunsplit(
            (
                parts.scheme.lower(),
                netloc,
                path,
                urlencode(query, safe="[]/:,@"),
                fragment,
            )
        )

    def _redact_fragment(self, text: str) -> tuple[str, Counter[str]]:
        hits: Counter[str] = Counter()
        for detector in self.detectors:
            text, n = detector.redact(text, "url")
            if n:
                hits[detector.name] += n
        return text, hits


def origin_only(url: str) -> str:
    """scheme://host/ for sensitive visits."""
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            return f"{parts.scheme.lower()}://{parts.hostname.lower()}/"
    except ValueError:
        pass
    return REDACTED
