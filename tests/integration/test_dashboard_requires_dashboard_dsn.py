"""``aslan dashboard serve`` requires ``ASLAN_DASHBOARD_DSN``.

Spec §6.1 + §8.2: the dashboard logs in to PostgreSQL as the
dedicated ``aslan_dashboard`` role created in migration 0020. The
DSN is a separate env var from ``ASLAN_PG_DSN`` (which points at
the application role with INSERT/UPDATE/DELETE) so an operator
cannot accidentally start the dashboard against a privileged
connection. Without the env var the CLI exits with a usage error
that points at the migration — operators who forget the role
should be one ``alembic upgrade`` and one env-var away from
recovery.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from aslan_core.cli.main import cli

pytestmark = pytest.mark.integration


def test_serve_without_dashboard_dsn_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASLAN_DASHBOARD_DSN", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "serve"])
    assert result.exit_code != 0, result.output


def test_serve_without_dashboard_dsn_points_at_migration_0020(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator sees a message naming the migration that creates the
    role — turns the failure into a self-service recovery."""
    monkeypatch.delenv("ASLAN_DASHBOARD_DSN", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "serve"])
    assert "ASLAN_DASHBOARD_DSN" in result.output
    assert "0020" in result.output


def test_serve_with_dashboard_dsn_does_not_exit_for_dsn_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASLAN_DASHBOARD_DSN", "postgresql://aslan_dashboard@127.0.0.1/aslan")
    monkeypatch.setattr("aslan_core.dashboard.serve", lambda **_: None)
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "serve"])
    assert result.exit_code == 0, result.output
