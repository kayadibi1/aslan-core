"""Integration tests for ``aslan agg`` CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from aslan_core.cli.main import cli

pytestmark = pytest.mark.integration


@pytest.fixture()
def seed_file(tmp_path: Path) -> Path:
    data: dict[str, Any] = {
        "restatement_configs": [
            {
                "name": "tas29_cli_test",
                "cpi_series_code": "evds.macro.cpi.headline",
                "base_date": "2022-01-01",
                "applies_from": "2022-01-01",
                "applies_to": "9999-12-31",
                "method": "tas29_monthly_cpi",
                "description": "CLI test config",
            }
        ]
    }
    f = tmp_path / "restatement.yaml"
    f.write_text(yaml.dump(data), encoding="utf-8")
    return f


class TestAggRefreshSnapshot:
    def test_refresh_snapshot_succeeds(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["agg", "refresh-snapshot"])
        assert result.exit_code == 0
        assert "rows" in result.output.lower()


class TestAggSeedRestatement:
    def test_seed_creates_config(self, seed_file: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["agg", "seed-restatement", "--file", str(seed_file)])
        assert result.exit_code == 0
        assert "tas29_cli_test" in result.output

    def test_seed_idempotent(self, seed_file: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["agg", "seed-restatement", "--file", str(seed_file)])
        result = runner.invoke(cli, ["agg", "seed-restatement", "--file", str(seed_file)])
        assert result.exit_code == 0
