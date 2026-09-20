"""CLI tests for the ``dashboard`` subcommand.

The server is never actually started here: the command is checked for argument
parsing and for the loopback safety gate, and the serving function is
monkeypatched so the test asserts *what would be served* without opening a
socket.
"""

from __future__ import annotations

import pytest

from lead_finder_agent.cli import build_parser, cmd_dashboard, main


class TestDashboardParser:
    def test_dashboard_command_parses(self):
        args = build_parser().parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert args.host is None
        assert args.port is None
        assert args.open_browser is False
        assert args.allow_remote is False

    def test_host_and_port_are_accepted(self):
        args = build_parser().parse_args(["dashboard", "--host", "127.0.0.1", "--port", "9000"])
        assert args.host == "127.0.0.1"
        assert args.port == 9000

    def test_open_flag_is_accepted(self):
        args = build_parser().parse_args(["dashboard", "--open"])
        assert args.open_browser is True


class TestDashboardSafetyGate:
    def test_non_loopback_host_is_refused_without_opt_in(self, capsys):
        code = main(["dashboard", "--host", "0.0.0.0"])
        captured = capsys.readouterr()
        assert code == 2
        assert "refusing to bind" in captured.err
        assert "--allow-remote" in captured.err

    def test_public_host_is_refused(self, capsys):
        code = main(["dashboard", "--host", "192.168.1.10"])
        assert code == 2
        assert "refusing to bind" in capsys.readouterr().err

    def test_loopback_host_is_allowed(self, monkeypatch, tmp_path):
        served = {}

        def fake_serve(service, *, host, port, open_browser=False):
            served.update(host=host, port=port, open_browser=open_browser)

        monkeypatch.setattr("lead_finder_agent.dashboard.server.serve", fake_serve)
        code = main(
            ["--db", str(tmp_path / "x.db"), "dashboard", "--host", "127.0.0.1", "--port", "0"]
        )
        assert code == 0
        assert served["host"] == "127.0.0.1"
        assert served["port"] == 0

    def test_allow_remote_permits_a_non_loopback_bind(self, monkeypatch, tmp_path):
        served = {}

        def fake_serve(service, *, host, port, open_browser=False):
            served.update(host=host, port=port)

        monkeypatch.setattr("lead_finder_agent.dashboard.server.serve", fake_serve)
        code = main(
            [
                "--db",
                str(tmp_path / "x.db"),
                "dashboard",
                "--host",
                "0.0.0.0",
                "--port",
                "0",
                "--allow-remote",
            ]
        )
        assert code == 0
        assert served["host"] == "0.0.0.0"

    def test_default_port_is_used_when_omitted(self, monkeypatch, tmp_path):
        from lead_finder_agent.dashboard.server import DEFAULT_PORT

        served = {}
        monkeypatch.setattr(
            "lead_finder_agent.dashboard.server.serve",
            lambda service, *, host, port, open_browser=False: served.update(port=port),
        )
        main(["--db", str(tmp_path / "x.db"), "dashboard"])
        assert served["port"] == DEFAULT_PORT

    def test_the_specified_database_is_the_one_served(self, monkeypatch, tmp_path):
        """--db must reach the dashboard's settings, not be ignored."""
        db = tmp_path / "cli-dashboard.db"
        captured = {}

        def fake_serve(service, *, host, port, open_browser=False):
            captured["db_path"] = str(service.effective_settings().db_path)

        monkeypatch.setattr("lead_finder_agent.dashboard.server.serve", fake_serve)
        main(["--db", str(db), "dashboard", "--port", "0"])
        assert captured["db_path"] == str(db.resolve())


class TestCmdDashboardUnit:
    def test_cmd_dashboard_returns_zero_on_loopback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "lead_finder_agent.dashboard.server.serve",
            lambda service, *, host, port, open_browser=False: None,
        )
        args = build_parser().parse_args(
            ["--db", str(tmp_path / "x.db"), "dashboard", "--port", "0"]
        )
        assert cmd_dashboard(args) == 0