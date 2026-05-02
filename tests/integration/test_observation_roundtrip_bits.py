"""Codex F10 + F11 + F12 + F13 — IEEE-754 bit-preservation contract.

Verifies that ``ts.observation.value`` stores binary64 EXACTLY, with
sign of zero, NaN bit pattern, subnormals, and infinities all
surviving the INSERT → SELECT round-trip byte-identical.

Per spec §2 Phase-2 Storage and read-back contract (codex F10): the
contract is meaningful only if ``DOUBLE PRECISION`` round-trips bits
without canonicalisation across CPython, Pydantic, asyncpg, and
Postgres. The 8 byte-pinned vectors below are non-negotiable;
implementers may add more, never remove.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.schemas.timeseries import ObservationIn
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


# Each entry: (name, 8-byte LE payload, the resulting Python float for INSERT).
BIT_PRESERVATION_VECTORS: list[tuple[str, bytes, float]] = [
    # +0.0 = 0x0000000000000000
    ("positive_zero", bytes.fromhex("0000000000000000"), 0.0),
    # -0.0 = 0x8000000000000000  → LE bytes ending in 0x80 0x00
    ("negative_zero", bytes.fromhex("0000000000000080"), -0.0),
    # Canonical positive qNaN = 0x7FF8000000000000  → LE ending in 0xF8 0x7F
    ("canonical_qnan", bytes.fromhex("000000000000f87f"), float("nan")),
    # Non-canonical NEGATIVE qNaN = 0xFFF8000000000001  → LE ending in 0xF8 0xFF
    (
        "noncanonical_qnan",
        bytes.fromhex("010000000000f8ff"),
        struct.unpack("<d", bytes.fromhex("010000000000f8ff"))[0],
    ),
    # Signaling NaN (negative) = 0xFFF0000000000001  → LE ending in 0xF0 0xFF
    (
        "signaling_nan",
        bytes.fromhex("010000000000f0ff"),
        struct.unpack("<d", bytes.fromhex("010000000000f0ff"))[0],
    ),
    # Smallest positive subnormal = 0x0000000000000001  → LE 0x01 first byte
    (
        "smallest_subnormal",
        bytes.fromhex("0100000000000000"),
        struct.unpack("<d", bytes.fromhex("0100000000000000"))[0],
    ),
    # +Infinity = 0x7FF0000000000000  → LE ending in 0xF0 0x7F
    ("positive_infinity", bytes.fromhex("000000000000f07f"), float("inf")),
    # -Infinity = 0xFFF0000000000000  → LE ending in 0xF0 0xFF
    ("negative_infinity", bytes.fromhex("000000000000f0ff"), -float("inf")),
]


def _assert_is_nan_pattern(payload: bytes) -> None:
    """Verify the payload is actually an IEEE-754 NaN: exponent
    all-ones AND mantissa not all zero (otherwise it would be
    ±Infinity)."""
    sign_and_exp = (payload[7] << 4) | (payload[6] >> 4)
    exp_only = sign_and_exp & 0x7FF
    assert exp_only == 0x7FF, f"NaN must have all-ones exponent; got {exp_only:#x}"
    mantissa = bytes(payload[:6]) + bytes([payload[6] & 0x0F])
    assert any(b != 0 for b in mantissa), "NaN must have non-zero mantissa; got infinity instead"


def test_nan_fixtures_are_real_nans() -> None:
    """Sanity check the byte-pinned NaN vectors before any DB I/O.
    Codex F12 + F13: an earlier draft had byte-swapped patterns that
    were finite negative numbers, not NaN — the assertion below catches
    that immediately."""
    for name in ("canonical_qnan", "noncanonical_qnan", "signaling_nan"):
        _, payload, _ = next(v for v in BIT_PRESERVATION_VECTORS if v[0] == name)
        _assert_is_nan_pattern(payload)


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.observation_batch_keys",
        "DELETE FROM ts.observation",
        "DELETE FROM ts.series_subject",
        "DELETE FROM ts.series_catalog",
        "DELETE FROM audit.events",
    ]:
        await session.execute(text(stmt))
    await session.commit()


@pytest.fixture(autouse=True)
async def _cleanup_ts(session: AsyncSession) -> AsyncIterator[None]:
    yield
    await session.rollback()
    await _wipe(session)


async def _seed(session: AsyncSession) -> int:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', 'roundtrip') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "name,le_bytes,value",
    BIT_PRESERVATION_VECTORS,
    ids=[v[0] for v in BIT_PRESERVATION_VECTORS],
)
async def test_value_round_trips_bit_identical(
    session: AsyncSession,
    name: str,
    le_bytes: bytes,
    value: float,
) -> None:
    """Codex F15, 2026-04-29: staged bit-preservation assertions.

    Asserts byte-identical round-trip at FOUR stages so a future
    regression is localised to the exact layer that broke:
      1. After ``float()`` construction (CPython preservation).
      2. After ``ObservationIn(value=...)`` validation (Pydantic).
      3. After ``obs.payload_hash()`` recomputation on fresh inputs
         (struct.pack must hash the original bytes).
      4. After INSERT -> SELECT via asyncpg + DOUBLE PRECISION.

    If a stage fails, do NOT paper over it in the writer. Update the
    spec to declare the canonicalisation, ratchet the contract, and
    drop the affected vector with a documented justification.
    """
    # ---- Stage 1: CPython float construction ------------------------
    stage1_bytes = struct.pack("<d", value)
    assert stage1_bytes == le_bytes, (
        f"{name}: CPython float() canonicalised the bit pattern.\n"
        f"  expected: {le_bytes.hex()}\n"
        f"  got:      {stage1_bytes.hex()}"
    )

    # ---- Stage 2: ObservationIn / Pydantic validation ---------------
    rid = await _seed(session)
    set_actor(Actor(actor_id="user:roundtrip", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code=f"rt.{name}",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="x",
    )
    await session.commit()

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    obs = ObservationIn(ts=ts, as_of=as_of, value=value)
    stage2_bytes = struct.pack("<d", obs.value)
    assert stage2_bytes == le_bytes, (
        f"{name}: Pydantic ObservationIn validation canonicalised the bit pattern.\n"
        f"  expected: {le_bytes.hex()}\n"
        f"  got:      {stage2_bytes.hex()}"
    )

    # ---- Stage 3: payload_hash input ---------------------------------
    expected_hash = obs.payload_hash()
    # A fresh ObservationIn constructed from the SAME raw bytes must
    # produce the same payload_hash — proves payload_hash hashes the
    # binary64 bits, not a canonicalised float repr.
    obs_dup = ObservationIn(ts=ts, as_of=as_of, value=struct.unpack("<d", le_bytes)[0])
    assert obs_dup.payload_hash() == expected_hash, (
        f"{name}: payload_hash differs across two ObservationIns "
        f"constructed from identical bit patterns — payload_hash MUST "
        f"NOT canonicalize NaN/zero bits."
    )

    # ---- Stage 4: DB round-trip via asyncpg + DOUBLE PRECISION ------
    await w.write(sr.series_id, [obs])
    await session.commit()

    # Read back the value AS DOUBLE PRECISION (asyncpg returns a Python float
    # whose IEEE-754 bits match the stored binary64 exactly).
    read = await session.scalar(
        text("SELECT value FROM ts.observation WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )
    assert isinstance(read, float)
    stage4_bytes = struct.pack("<d", read)
    assert stage4_bytes == le_bytes, (
        f"{name}: round-trip changed bits.\n"
        f"  inserted:  {le_bytes.hex()}\n"
        f"  read-back: {stage4_bytes.hex()}"
    )

    # Recomputed payload_hash on a fresh ObservationIn from the read-back
    # value must equal the original hash.
    o2 = ObservationIn(ts=ts, as_of=as_of, value=read)
    assert o2.payload_hash() == expected_hash
