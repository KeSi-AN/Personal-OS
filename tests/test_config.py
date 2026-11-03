from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_os.config.loader import config_path, defaults_text, load_config, set_value
from personal_os.config.schema import ApiConfig, Config


def test_first_run_writes_commented_defaults(home: Path):
    cfg = load_config(home)
    text = config_path(home).read_text("utf-8")
    assert "# Personal OS configuration." in text
    assert cfg.data_dir == home
    assert cfg.watched_roots == [Path("~/Documents").expanduser()]
    assert cfg.observers.fs.enabled is False and cfg.observers.window.enabled is True
    assert "instagram.com" in cfg.privacy.blocked_domains
    assert cfg.llm.fast_model == cfg.llm.reasoning_model == "qwen3:4b"
    assert cfg.actions.auto_allow_tiers == [0, 1]


def test_defaults_match_schema_exactly():
    Config.model_validate({**tomllib.loads(defaults_text()), "data_dir": "x"})  # extra=forbid


def test_set_value_preserves_comments_and_validates(home: Path):
    load_config(home)
    cfg = set_value("observers.idle.threshold_s", 600, home)
    assert cfg.observers.idle.threshold_s == 600
    text = config_path(home).read_text("utf-8")
    assert "threshold_s = 600" in text and "# no input for this long" in text
    with pytest.raises(ValidationError):
        set_value("observers.idle.threshold_s", 1, home)  # below minimum: file untouched
    assert "threshold_s = 600" in config_path(home).read_text("utf-8")
    with pytest.raises(KeyError):
        set_value("observers.nope.x", 1, home)


def test_core_api_cannot_bind_to_the_network():
    with pytest.raises(ValidationError):
        ApiConfig(host="0.0.0.0")


def test_tier_two_cannot_be_auto_allowed(home: Path):
    load_config(home)
    with pytest.raises(ValidationError):
        set_value("actions.auto_allow_tiers", [0, 1, 2], home)


def test_settings_added_later_come_from_defaults(home: Path):
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # an old config written before no_text_domains existed, with one customised value
    path.write_text('[privacy]\nsensitive_domains = ["mail.example"]\n', encoding="utf-8")
    cfg = load_config(home)
    assert cfg.privacy.sensitive_domains == ["mail.example"]  # yours kept
    assert "docs.google.com" in cfg.privacy.no_text_domains  # new default filled in
    assert "instagram.com" in cfg.privacy.blocked_domains
