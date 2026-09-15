"""Tests for the CLI.

The CLI is exercised end-to-end through :func:`lead_finder_agent.cli.main` with
the offline ``sample`` provider, so no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_finder_agent.cli import build_parser, main
from lead_finder_agent.storage import SQLiteLeadRepository


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "cli.db")


def run_cli(*args: str) -> int:
    return main(list(args))


class TestParser:
    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0

    def test_requires_a_command(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_search_defaults(self):
        args = build_parser().parse_args(["search", "--city", "Aden"])
        assert args.limit == 50
        assert args.city == "Aden"
        assert args.no_website_check is False


class TestSearchCommand:
    def test_search_with_sample_provider(self, db_path, capsys):
        code = run_cli(
            "--db", db_path, "search",
            "--city", "Aden",
            "--providers", "sample",
            "--no-website-check",
            "--limit", "5",
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Business" in out
        assert "lead(s) found" in out

    def test_search_persists_to_database(self, db_path):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--providers", "sample",
            "--no-website-check", "--limit", "3",
        )
        repo = SQLiteLeadRepository(db_path)
        assert repo.count() == 3
        repo.close()

    def test_search_json_output_is_valid(self, db_path, capsys):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--providers", "sample",
            "--no-website-check", "--limit", "2", "--json",
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["query"]["city"] == "Aden"
        assert len(payload["leads"]) == 2

    def test_search_with_no_store_keeps_database_empty(self, db_path):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--providers", "sample",
            "--no-website-check", "--no-store", "--limit", "3",
        )
        repo = SQLiteLeadRepository(db_path)
        assert repo.count() == 0
        repo.close()

    def test_search_show_stats(self, db_path, capsys):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--providers", "sample",
            "--no-website-check", "--limit", "2", "--show-stats",
        )
        assert "Pipeline summary" in capsys.readouterr().out

    def test_search_unknown_city_reports_no_leads(self, db_path, capsys):
        run_cli(
            "--db", db_path, "search",
            "--city", "Atlantis", "--providers", "sample",
            "--no-website-check", "--limit", "2",
        )
        assert "No leads found." in capsys.readouterr().out

    def test_search_with_unknown_provider_warns_but_succeeds(self, db_path, capsys):
        code = run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--providers", "nope",
            "--no-website-check", "--limit", "2",
        )
        assert code == 0
        assert "No leads found." in capsys.readouterr().out

    def test_business_type_filter(self, db_path, capsys):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--type", "bakery",
            "--providers", "sample", "--no-website-check", "--limit", "10",
        )
        out = capsys.readouterr().out
        assert "Golden Star Bakery" in out
        assert "Modern Electronics" not in out


class TestListCommand:
    @pytest.fixture(autouse=True)
    def seed(self, db_path):
        run_cli(
            "--db", db_path, "search",
            "--city", "Aden", "--type", "bakery",
            "--providers", "sample", "--no-website-check", "--limit", "10",
        )

    def test_list_shows_rows(self, db_path, capsys):
        assert run_cli("--db", db_path, "list", "--limit", "5") == 0
        assert "Golden Star Bakery" in capsys.readouterr().out

    def test_list_json(self, db_path, capsys):
        run_cli("--db", db_path, "list", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert payload and payload[0]["business_name"]

    def test_list_min_score_filter_excludes_low_scores(self, db_path, capsys):
        run_cli("--db", db_path, "list", "--min-score", "99")
        assert "No stored leads match those filters." in capsys.readouterr().out

    def test_list_on_empty_database(self, tmp_path, capsys):
        empty = str(tmp_path / "empty.db")
        run_cli("--db", empty, "list")
        assert "No stored leads match those filters." in capsys.readouterr().out

    def test_list_ascending_order(self, db_path, capsys):
        run_cli("--db", db_path, "list", "--ascending", "--json")
        payload = json.loads(capsys.readouterr().out)
        scores = [lead["lead_score"] for lead in payload]
        assert scores == sorted(scores)


class TestShowCommand:
    def test_show_existing_lead(self, db_path, capsys):
        run_cli(
            "--db", db_path, "search", "--city", "Aden", "--type", "bakery",
            "--providers", "sample", "--no-website-check", "--limit", "1",
        )
        repo = SQLiteLeadRepository(db_path)
        lead_id = repo.find()[0].id
        repo.close()

        assert run_cli("--db", db_path, "show", lead_id) == 0
        out = capsys.readouterr().out
        assert "Business" in out
        assert "Score" in out
        assert "Reasons:" in out

    def test_show_missing_lead_returns_error(self, db_path, capsys):
        assert run_cli("--db", db_path, "show", "does-not-exist") == 1
        assert "No lead found" in capsys.readouterr().err


class TestExportCommand:
    @pytest.fixture(autouse=True)
    def seed(self, db_path):
        run_cli(
            "--db", db_path, "search", "--city", "Aden",
            "--providers", "sample", "--no-website-check", "--limit", "5",
        )

    def test_export_json(self, db_path, tmp_path, capsys):
        target = tmp_path / "leads.json"
        assert run_cli("--db", db_path, "export", "--format", "json", "--output", str(target)) == 0
        payload = json.loads(target.read_text())
        assert len(payload["leads"]) == 5
        assert "Exported to" in capsys.readouterr().out

    def test_export_csv(self, db_path, tmp_path):
        target = tmp_path / "leads.csv"
        run_cli("--db", db_path, "export", "--format", "csv", "--output", str(target))
        assert target.read_text().startswith("id,business_name")

    def test_export_infers_format_when_omitted(self, db_path, tmp_path):
        target = tmp_path / "leads.csv"
        run_cli("--db", db_path, "export", "--output", str(target))
        assert target.read_text().startswith("id,business_name")

    def test_export_respects_filters(self, db_path, tmp_path):
        target = tmp_path / "filtered.json"
        run_cli(
            "--db", db_path, "export", "--output", str(target),
            "--min-score", "999",
        )
        assert json.loads(target.read_text())["leads"] == []


class TestOtherCommands:
    def test_stats(self, db_path, capsys):
        assert run_cli("--db", db_path, "stats") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 0

    def test_providers_lists_builtins(self, capsys):
        assert run_cli("providers") == 0
        out = capsys.readouterr().out
        assert "osm" in out
        assert "sample" in out

    def test_unknown_command_exits_nonzero(self):
        with pytest.raises(SystemExit):
            run_cli("frobnicate")

    def test_db_flag_overrides_settings_env(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "explicit.db"
        run_cli("--db", str(target), "search", "--city", "Aden",
                "--providers", "sample", "--no-website-check", "--limit", "1")
        assert target.exists()