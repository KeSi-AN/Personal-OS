"""What the resolver looks at for one stored event: paths, site, app and title, words."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from personal_os.core.clock import from_ms
from personal_os.projects.registry import norm_path

BROWSER_EXES = {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "opera.exe", "arc.exe"}
EDITOR_EXES = {
    "code.exe",
    "cursor.exe",
    "idea64.exe",
    "pycharm64.exe",
    "webstorm64.exe",
    "devenv.exe",
    "sublime_text.exe",
    "notepad++.exe",
    "zed.exe",
    "rider64.exe",
}
NO_PROJECT_SOURCES = {"system", "idle", "meta"}

_WORD_RE = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_CHUNK_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"\s+[-–—|·:]\s+")
STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "what",
        "how",
        "why",
        "when",
        "your",
        "you",
        "are",
        "was",
        "were",
        "have",
        "has",
        "not",
        "but",
        "can",
        "will",
        "into",
        "using",
        "use",
        "out",
        "about",
        "all",
        "new",
        "tab",
        "page",
        "home",
        "search",
        "google",
        "results",
        "result",
        "chrome",
        "brave",
        "edge",
        "firefox",
        "browser",
        "window",
        "windows",
        "file",
        "explorer",
        "visual",
        "studio",
        "code",
        "cursor",
        "powershell",
        "terminal",
        "bash",
        "command",
        "youtube",
        "video",
        "watch",
        "http",
        "https",
        "www",
        "com",
        "org",
        "net",
        "html",
        "inbox",
        "mail",
        "gmail",
        "docs",
        "document",
        "untitled",
        "redacted",
        "main",
        "master",
        "json",
        "yaml",
        "toml",
        "txt",
        "md",
        "tsx",
        "jsx",
        "py",
        "ts",
        "js",
        "css",
        "test",
        "tests",
        "src",
        "lib",
        "app",
        "apps",
        "index",
        "readme",
        "open",
        "click",
        "settings",
    ]
)


# Folder names every path on this machine shares (C:/Users/<you>/Documents/...): noise for
# telling projects apart.
PATH_STOPWORDS = frozenset(
    {
        "users",
        "documents",
        "appdata",
        "local",
        "roaming",
        "desktop",
        "downloads",
        "program",
        "files",
        "home",
        "onedrive",
        "temp",
        "venv",
        "scripts",
        "site",
        "packages",
    }
    | {w.lower() for w in _WORD_RE.findall(Path.home().name)}
)


def tokens(text: str) -> list[str]:
    """Words for keyword matching: split camelCase, snake_case and paths; drop noise."""
    out = []
    for chunk in _CHUNK_RE.findall(text or ""):
        # split camelCase at lower->upper only, so acronyms stay whole ("OAuth" -> oauth,
        # "refreshToken" -> refresh, token)
        for w in _CAMEL_RE.sub(" ", chunk).split():
            w = w.lower()
            if len(w) >= 3 and w not in STOPWORDS and w not in PATH_STOPWORDS and not w.isdigit():
                out.append(w)
    return out


def title_segments(title: str) -> list[str]:
    """'notes.md - my-repo - Visual Studio Code' -> its ' - ' parts."""
    return [s.strip() for s in _SPLIT_RE.split(title or "") if s.strip()]


@dataclass
class Features:
    id: str
    source: str
    type: str
    start: datetime
    end: datetime
    privacy: str
    assign_source: str | None
    project_id: str | None
    confidence: float | None
    paths: list[tuple[str, str]] = field(default_factory=list)  # (normalized path, role)
    domain: str | None = None
    github: str | None = None  # "github.com/owner/repo"
    exe: str | None = None
    title: str = ""
    words: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def is_browser_window(self) -> bool:
        return self.source == "window" and (self.exe or "") in BROWSER_EXES

    def rule_keys(self) -> list[tuple[str, str]]:
        """Keys a correction of this event teaches, most specific first."""
        keys: list[tuple[str, str]] = []
        if self.github:
            keys.append(("github", self.github))
        if self.domain:
            keys.append(("domain", self.domain))
        for path, _role in self.paths:
            folder = path if self.source == "terminal" else str(PurePosixPath(path).parent)
            keys.append(("path", folder))
        if self.source == "window" and self.exe:
            first = self.first_segment()
            if first:
                keys.append(("app_title", f"{self.exe}|{first}"))
            if not self.is_browser_window:
                keys.append(("app", self.exe))
        return keys

    def first_segment(self) -> str | None:
        """The varying part of a window title: the folder in "Documents - File Explorer", or
        the site/app part of a page shown in a browser window ("Meet" in "Meet - abc-defg")."""
        title = self.title
        if self.is_browser_window:
            from personal_os.observers.window import browser_page_title

            title = browser_page_title(title)
            segs = title_segments(title)
            if segs and segs[0].lower() not in ("", "[redacted]"):
                return segs[0].lower()
            return None
        segs = title_segments(title)
        if len(segs) >= 2 and segs[0].lower() not in ("", "[redacted]"):
            return segs[0].lower()
        return None

    def inbox_key(self) -> tuple[str, str]:
        """How unassigned items are grouped in the inbox."""
        if self.domain:
            return ("domain", self.domain)
        if self.source == "window" and self.exe:
            # File Explorer differs by folder and browser windows by site; other apps (Claude,
            # Notepad) group by app.
            if (self.exe == "explorer.exe" or self.is_browser_window) and self.first_segment():
                return ("app_title", f"{self.exe}|{self.first_segment()}")
            return ("app", self.exe)
        if self.paths:
            path = self.paths[0][0]
            return ("path", path if self.source == "terminal" else str(PurePosixPath(path).parent))
        return ("event", self.id)


def build(
    row: Any, extract_title: str | None = None, extract_headings: str | None = None
) -> Features:
    """From an events row (id, source, type, ts_start, ts_end, last_seen, privacy_class,
    assign_source, project_id, project_confidence, payload)."""
    p = json.loads(row["payload"])
    start = from_ms(row["ts_start"])
    end = from_ms(row["ts_end"] or row["last_seen"] or row["ts_start"])
    f = Features(
        id=row["id"],
        source=row["source"],
        type=row["type"],
        start=start,
        end=end,
        privacy=row["privacy_class"],
        assign_source=row["assign_source"],
        project_id=row["project_id"],
        confidence=row["project_confidence"],
        duration_s=(end - start).total_seconds(),
    )
    roles = {
        "repo_root": "repo",
        "path": "file",
        "dest_path": "file",
        "cwd": "cwd",
        "workspace": "workspace",
        "file": "file",
        "dir": "file",
    }
    for key, role in roles.items():
        value = p.get(key)
        if isinstance(value, str) and value and value != "[redacted]":
            f.paths.append((norm_path(value), role))
    text_parts: list[str] = []
    if f.source == "browser":
        f.domain = (p.get("domain") or "").lower() or None
        enr = p.get("enrichment") or {}
        if isinstance(enr.get("repo"), str):
            f.github = f"github.com/{enr['repo'].lower()}"
        text_parts += [p.get("title", ""), p.get("search_query") or "", p.get("text", "")]
        if extract_title:
            text_parts.append(extract_title)
        if extract_headings:
            text_parts += json.loads(extract_headings)
        f.title = p.get("title", "")
    elif f.source == "window":
        f.exe = (p.get("exe") or "").lower() or None
        f.title = p.get("title", "")
        text_parts.append(f.title)
    elif f.source == "terminal":
        text_parts.append(p.get("command", ""))
    elif f.source == "git":
        # commit "files" are paths; state "files" are {path, status, added, removed} objects
        files = [x if isinstance(x, str) else x.get("path", "") for x in (p.get("files") or [])]
        text_parts += [p.get("message") or "", " ".join(f for f in files if isinstance(f, str))]
    elif f.source == "fs":
        text_parts += [
            PurePosixPath(norm_path(p.get("path", ""))).stem,
            *(p.get("symbols_added") or []),
            *(p.get("symbols_changed") or []),
        ]
    elif f.source == "editor":
        text_parts += [p.get("symbol_path") or "", p.get("file") or ""]
    f.words = tokens(" ".join(t for t in text_parts if isinstance(t, str)))
    return f
