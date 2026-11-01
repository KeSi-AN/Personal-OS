"""Compiled privacy rules (blocklists, allowlist, secret paths) built from PrivacyConfig."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from personal_os.config.schema import PrivacyConfig

# Schemes that are never captured by the browser source.
NON_WEB_SCHEMES = frozenset(
    {"chrome", "brave", "edge", "about", "chrome-extension", "moz-extension", "file", "devtools",
     "view-source", "data", "blob", "javascript"}
)  # fmt: skip


def _norm_path(path: str) -> str:
    """OS-agnostic, lower-cased form for comparisons (Windows paths are case-insensitive)."""
    return path.replace("\\", "/").rstrip("/").lower()


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def scheme_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _in_domains(domain: str, domains: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


@dataclass(frozen=True)
class PrivacyRules:
    blocked_apps: frozenset[str] = frozenset()
    blocked_domains: tuple[str, ...] = ()
    blocked_domain_keywords: tuple[str, ...] = ()
    sensitive_domains: tuple[str, ...] = ()
    allowlist_mode: bool = False
    allowed_domains: tuple[str, ...] = ()
    redact_title_res: tuple[re.Pattern[str], ...] = ()
    sensitive_keyword_re: re.Pattern[str] | None = None
    secret_globs: tuple[str, ...] = ()
    secret_dirs: frozenset[str] = frozenset()
    protected_dirs: tuple[str, ...] = field(default_factory=tuple)
    min_dwell_ms: int = 5000

    @classmethod
    def from_config(
        cls, cfg: PrivacyConfig, *, protected_dirs: list[Path] | None = None
    ) -> PrivacyRules:
        keyword_re = None
        if cfg.sensitive_keywords:
            alternation = "|".join(re.escape(k) for k in cfg.sensitive_keywords)
            keyword_re = re.compile(rf"(?i)\b(?:{alternation})\b")
        return cls(
            blocked_apps=frozenset(a.lower() for a in cfg.blocked_apps),
            blocked_domains=tuple(cfg.blocked_domains),
            blocked_domain_keywords=tuple(k.lower() for k in cfg.blocked_domain_keywords),
            sensitive_domains=tuple(cfg.sensitive_domains),
            allowlist_mode=cfg.allowlist_mode,
            allowed_domains=tuple(cfg.allowed_domains),
            redact_title_res=tuple(re.compile(p) for p in cfg.redact_title_patterns),
            sensitive_keyword_re=keyword_re,
            secret_globs=tuple(g.lower() for g in cfg.secret_path_globs),
            secret_dirs=frozenset(d.lower() for d in cfg.secret_dir_names),
            protected_dirs=tuple(_norm_path(str(p)) for p in (protected_dirs or [])),
            min_dwell_ms=int(cfg.min_dwell_s * 1000),
        )

    def is_blocked_app(self, *names: str | None) -> bool:
        return any(n and n.lower() in self.blocked_apps for n in names)

    def is_blocked_domain(self, domain: str) -> bool:
        if not domain:
            return False
        if _in_domains(domain, self.blocked_domains):
            return True
        labels = domain.split(".")[:-1]  # ignore the TLD
        if any(k in label for label in labels for k in self.blocked_domain_keywords):
            return True
        return self.allowlist_mode and not _in_domains(domain, self.allowed_domains)

    def is_sensitive_domain(self, domain: str) -> bool:
        return bool(domain) and _in_domains(domain, self.sensitive_domains)

    def title_needs_redaction(self, title: str) -> bool:
        return any(r.search(title) for r in self.redact_title_res)

    def is_secret_path(self, path: str | None) -> bool:
        if not path:
            return False
        p = PurePosixPath(_norm_path(path))
        if any(part in self.secret_dirs for part in p.parts):
            return True
        return any(fnmatch.fnmatchcase(p.name, g) for g in self.secret_globs)

    def is_protected_path(self, path: str | None) -> bool:
        if not path:
            return False
        n = _norm_path(path)
        return any(n == d or n.startswith(d + "/") for d in self.protected_dirs)
