"""Tests for configuration loading and settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_finder_agent.config import (
    Settings,
    get_settings,
    load_config_file,
    load_packaged_data,
    merge_dicts,
    reset_settings,
)
from lead_finder_agent.config.loader import resolve_path


class TestPackagedData:
    def test_scoring_rules_have_rules(self):
        data = load_packaged_data("scoring_rules")
        assert data["rules"]
        assert data["score_max"] == 100

    def test_business_types_include_target_verticals(self):
        data = load_packaged_data("business_types")
        categories = data["categories"]
        for expected in ("restaurants", "clothing", "car_repair", "electronics"):
            assert expected in categories
            assert categories[expected]["tags"]

    def test_website_signals_loaded(self):
        data = load_packaged_data("website_signals")
        assert "facebook.com" in data["social_domains"]
        assert data["min_content_length"] > 0


class TestConfigFile:
    def test_load_json(self, tmp_path: Path):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({"score_max": 200}))
        assert load_config_file(path)["score_max"] == 200

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config_file(tmp_path / "missing.json")

    def test_unsupported_extension_raises(self, tmp_path: Path):
        path = tmp_path / "cfg.txt"
        path.write_text("hello")
        with pytest.raises(ValueError):
            load_config_file(path)


class TestMergeDicts:
    def test_recursive_merge_does_not_mutate_inputs(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        override = {"nested": {"y": 99, "z": 3}}
        merged = merge_dicts(base, override)
        assert merged == {"a": 1, "nested": {"x": 1, "y": 99, "z": 3}}
        assert base["nested"]["y"] == 2


class TestSettings:
    def test_defaults_are_usable(self):
        settings = Settings()
        assert settings.providers
        assert settings.default_city
        assert settings.db_path.name.endswith(".db")

    def test_from_env_reads_overrides(self):
        env = {
            "LEAD_FINDER_DB_PATH": "/tmp/custom.db",
            "LEAD_FINDER_PROVIDERS": "sample",
            "LEAD_FINDER_DEFAULT_CITY": "Sanaa",
            "LEAD_FINDER_HTTP_TIMEOUT": "5",
            "LEAD_FINDER_LOG_LEVEL": "debug",
        }
        settings = Settings.from_env(env)
        assert settings.providers == ["sample"]
        assert settings.default_city == "Sanaa"
        assert settings.http_timeout == 5.0
        assert settings.log_level == "DEBUG"

    def test_with_overrides_returns_copy(self, settings):
        other = settings.with_overrides(default_city="Taiz")
        assert other.default_city == "Taiz"
        assert settings.default_city == "Aden"
        assert other.db_path == settings.db_path

    def test_ensure_directories_creates_parent(self, tmp_path: Path):
        settings = Settings(db_path=tmp_path / "nested" / "dir" / "leads.db")
        settings.ensure_directories()
        assert settings.db_path.parent.is_dir()

    def test_get_settings_is_cached_until_reset(self):
        first = get_settings()
        assert get_settings() is first
        reset_settings()
        assert get_settings() is not first


class TestResolvePath:
    def test_absolute_path_is_kept(self, tmp_path: Path):
        assert resolve_path(tmp_path) == tmp_path

    def test_relative_path_is_anchored_to_project_root(self):
        resolved = resolve_path("data/leads.db")
        assert resolved.is_absolute()
        assert resolved.name == "leads.db"
