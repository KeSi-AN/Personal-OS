"""Load, create and edit config.toml.

Resolution order for the data directory:
  1. PERSONAL_OS_HOME environment variable (used by tests and the simulator)
  2. `data_dir` in config.toml
  3. %LOCALAPPDATA%\\PersonalOS (platformdirs user data dir)

config.toml itself always lives in the data directory chosen by 1 or 3.
"""

from __future__ import annotations

import os
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

import tomlkit
from platformdirs import user_data_dir

from personal_os.config.schema import Config

ENV_HOME = "PERSONAL_OS_HOME"
CONFIG_FILENAME = "config.toml"


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path(user_data_dir("PersonalOS", appauthor=False, roaming=False))


def defaults_text() -> str:
    return resources.files("personal_os.config").joinpath("defaults.toml").read_text("utf-8")


def config_path(home: Path | None = None) -> Path:
    return (home or default_home()) / CONFIG_FILENAME


def load_config(home: Path | None = None, *, create: bool = True) -> Config:
    """Load config.toml from `home`, writing the commented defaults on first run."""
    home = home or default_home()
    path = config_path(home)
    if not path.exists():
        if not create:
            return _build(tomllib.loads(defaults_text()), home)
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(defaults_text(), encoding="utf-8")
    return _build(tomllib.loads(path.read_text("utf-8")), home)


def _merge(defaults: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Your values win; settings added in a newer version come from the defaults."""
    out = dict(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _build(raw: dict[str, Any], home: Path) -> Config:
    raw = _merge(tomllib.loads(defaults_text()), raw)
    data_dir = raw.get("data_dir") or ""
    raw["data_dir"] = Path(data_dir).expanduser() if data_dir else home
    raw["watched_roots"] = [Path(p).expanduser() for p in raw.get("watched_roots", [])]
    return Config.model_validate(raw)


def set_value(dotted_key: str, value: Any, home: Path | None = None) -> Config:
    """Change one setting in config.toml, preserving comments, and return the validated result.

    The file is only rewritten if the new config validates.
    """
    path = config_path(home)
    if not path.exists():
        load_config(home)
    doc = tomlkit.parse(path.read_text("utf-8"))
    *parents, leaf = dotted_key.split(".")
    # The key must exist in the defaults; your file may predate it (settings added in a newer
    # version), in which case its section and key are added here.
    known: Any = tomllib.loads(defaults_text())
    for part in [*parents, leaf]:
        if not isinstance(known, dict) or part not in known:
            raise KeyError(f"unknown config key: {dotted_key}")
        known = known[part]
    table: Any = doc
    for part in parents:
        if part not in table:
            table[part] = tomlkit.table()
        table = table[part]
    table[leaf] = value
    text = tomlkit.dumps(doc)
    config = _build(tomllib.loads(text), home or default_home())
    path.write_text(text, encoding="utf-8")
    return config


def ensure_dirs(config: Config) -> None:
    for d in (config.data_dir, config.logs_dir, config.spool_dir, config.runtime_dir):
        d.mkdir(parents=True, exist_ok=True)
