from __future__ import annotations

import pytest
from click.testing import CliRunner

from aslan_core.cli.main import cli


def test_api_serve_help() -> None:
    result = CliRunner().invoke(cli, ["api", "serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output


def test_api_serve_rejects_missing_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASLAN_JWT_SECRET", raising=False)
    result = CliRunner().invoke(cli, ["api", "serve"])
    assert result.exit_code != 0
    assert "ASLAN_JWT_SECRET" in result.output
