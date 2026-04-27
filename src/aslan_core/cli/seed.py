from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import click
import yaml
from sqlalchemy import text

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory

CURRENCIES: list[tuple[str, str, int]] = [
    ("TRY", "Turkish Lira", 2),
    ("USD", "US Dollar", 2),
    ("EUR", "Euro", 2),
    ("GBP", "British Pound", 2),
    ("JPY", "Japanese Yen", 0),
]

# Minimal v0.1 BIST sector seed. Full taxonomy lands when BIST data
# integration begins; spec §3.6 says manual is acceptable for v0.1.
#
# RUF001 below is suppressed line-by-line: ruff treats Turkish
# dotless-i / dotted-I as "ambiguous", but per project policy
# (CLAUDE.md) Turkish text is preserved verbatim.
BIST_SECTORS: list[tuple[str, str, str, str, str]] = [
    ("bist:XBANK", "bist", "XBANK", "Bankalar", "Banks"),
    ("bist:XSGRT", "bist", "XSGRT", "Sigorta", "Insurance"),
    ("bist:XELKT", "bist", "XELKT", "Elektrik", "Electricity"),
    ("bist:XGIDA", "bist", "XGIDA", "Gıda, İçecek", "Food & Beverage"),  # noqa: RUF001
    ("bist:XHOLD", "bist", "XHOLD", "Holding ve Yatırım", "Holding & Investment"),  # noqa: RUF001
    ("bist:XILTM", "bist", "XILTM", "İletişim", "Communications"),
    ("bist:XKMYA", "bist", "XKMYA", "Kimya, Petrol, Plastik", "Chemicals/Petroleum/Plastics"),
    ("bist:XMANA", "bist", "XMANA", "Madencilik", "Mining"),
    ("bist:XMESY", "bist", "XMESY", "Metal Eşya, Makine", "Metal Products & Machinery"),
    ("bist:XTEKS", "bist", "XTEKS", "Tekstil, Deri", "Textile, Leather"),
    ("bist:XTRZM", "bist", "XTRZM", "Turizm", "Tourism"),
    ("bist:XULAS", "bist", "XULAS", "Ulaştırma", "Transport"),  # noqa: RUF001
]


@click.group()
def seed() -> None:
    """Seed reference data."""


@seed.command("currencies")
def seed_currencies() -> None:
    """Seed ref.currency with the v0.1 minor-unit table."""
    asyncio.run(_seed_currencies())


@seed.command("sectors")
@click.option("--taxonomy", required=True)
def seed_sectors(taxonomy: str) -> None:
    """Seed ref.sector for a taxonomy. v0.1 supports 'bist' only."""
    if taxonomy != "bist":
        raise click.UsageError(f"taxonomy={taxonomy!r} not supported in v0.1 (only 'bist')")
    asyncio.run(_seed_sectors_bist())


@seed.command("sources")
@click.option(
    "--file",
    "path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
)
def seed_sources(path: str) -> None:
    """Seed src.source from a YAML manifest."""
    # Read the YAML synchronously here (outside the event loop) so the
    # async helper does no blocking filesystem I/O.
    doc = cast(dict[str, Any], yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    rows: list[dict[str, Any]] = doc.get("sources", [])
    asyncio.run(_seed_sources(rows))


async def _seed_currencies() -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            for code, name, minor in CURRENCIES:
                await s.execute(
                    text(
                        "INSERT INTO ref.currency (currency_code, name, minor_unit) "
                        "VALUES (:c, :n, :m) "
                        "ON CONFLICT (currency_code) DO UPDATE "
                        "  SET name = EXCLUDED.name, minor_unit = EXCLUDED.minor_unit"
                    ),
                    {"c": code, "n": name, "m": minor},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def _seed_sectors_bist() -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            for sid, tax, code, ntr, nen in BIST_SECTORS:
                await s.execute(
                    text(
                        "INSERT INTO ref.sector "
                        "  (sector_id, taxonomy, code, name_tr, name_en) "
                        "VALUES (:sid, :tax, :code, :ntr, :nen) "
                        "ON CONFLICT (sector_id) DO UPDATE "
                        "  SET taxonomy = EXCLUDED.taxonomy, code = EXCLUDED.code, "
                        "      name_tr = EXCLUDED.name_tr, name_en = EXCLUDED.name_en"
                    ),
                    {"sid": sid, "tax": tax, "code": code, "ntr": ntr, "nen": nen},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def _seed_sources(rows: list[dict[str, Any]]) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as s:
            for r in rows:
                await s.execute(
                    text(
                        "INSERT INTO src.source "
                        "  (source_id, name, kind, base_url, license_status, metadata) "
                        "VALUES (:id, :name, :kind, :url, :lic, '{}'::jsonb) "
                        "ON CONFLICT (source_id) DO UPDATE "
                        "  SET name = EXCLUDED.name, kind = EXCLUDED.kind, "
                        "      base_url = EXCLUDED.base_url, "
                        "      license_status = EXCLUDED.license_status"
                    ),
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "kind": r["kind"],
                        "url": r.get("base_url"),
                        "lic": r["license_status"],
                    },
                )
            await s.commit()
    finally:
        await engine.dispose()
