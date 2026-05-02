"""Unit tests for agg ORM models and Pydantic schema."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from aslan_core.schemas.agg import RestatementConfigSchema


class TestRestatementConfigSchema:
    def test_valid_schema(self) -> None:
        schema = RestatementConfigSchema(
            config_id=1,
            name="tas29_tr_2022_onwards",
            cpi_series_code="evds.macro.cpi.headline",
            base_date=date(2022, 1, 1),
            applies_from=date(2022, 1, 1),
            applies_to=date(9999, 12, 31),
            method="tas29_monthly_cpi",
            description="TAS 29 restatement",
            created_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert schema.name == "tas29_tr_2022_onwards"
        assert schema.method == "tas29_monthly_cpi"

    def test_description_optional(self) -> None:
        schema = RestatementConfigSchema(
            config_id=1,
            name="test",
            cpi_series_code="cpi",
            base_date=date(2022, 1, 1),
            applies_from=date(2022, 1, 1),
            applies_to=date(9999, 12, 31),
            method="simple",
            description=None,
            created_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert schema.description is None

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            RestatementConfigSchema(
                config_id=1,
                name="test",
                # missing cpi_series_code
                base_date=date(2022, 1, 1),
                applies_from=date(2022, 1, 1),
                applies_to=date(9999, 12, 31),
                method="simple",
                description=None,
                created_at=datetime(2026, 5, 2, tzinfo=UTC),
            )  # type: ignore[call-arg]


def test_agg_models_importable() -> None:
    from aslan_core.models.agg import (
        EntityLatestSnapshot,
        ObservationDailyToMonthly,
        RestatementConfig,
    )

    assert RestatementConfig.__tablename__ == "restatement_config"
    assert EntityLatestSnapshot.__tablename__ == "entity_latest_snapshot"
    assert ObservationDailyToMonthly.__tablename__ == "observation_daily_to_monthly"
