"""Integration tests — ``ObservationWriter.upsert_series`` PII guard.

Plan reference: v0.4.0 Task 11. Covers:

* Codex F4 — clear-text PII pattern in ``series_code`` / ``description``
  on identifying-class series → :class:`IdentifyingSeriesPiiInClearText`.
* Codex F18 — identifying-class series with empty ``subjects`` →
  :class:`IdentifyingSeriesMissingSubject`.
* Codex F18 + F19 — PII in free-form metadata (``notes`` /
  ``fields.email`` / nested) → :class:`IdentifyingSeriesMetadataPii`.
* Codex F20 — direct-DB UPDATE bypass scenario detected by
  ``find_pii_in_metadata`` (deletion-runtime backstop).
* Codex F24 — numeric-string object keys in metadata →
  :class:`MetadataSchemaViolation`. Applies to ALL ``pii_class`` values.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import (
    IdentifyingSeriesMetadataPii,
    IdentifyingSeriesMissingSubject,
    IdentifyingSeriesPiiInClearText,
    MetadataSchemaViolation,
)
from aslan_core.schemas.timeseries import SubjectRef
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


async def _seed_sources(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _new_run(session: AsyncSession) -> int:
    await _seed_sources(session)
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', 'test') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
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


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_email_in_code_rejected(
    session: AsyncSession,
) -> None:
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesPiiInClearText, match="series_code"):
        await w.upsert_series(
            series_code="exec.cmptn.john.doe@aselsan.com",
            source_id="kap",
            metric="m",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_full_name_in_description_rejected(
    session: AsyncSession,
) -> None:
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesPiiInClearText, match="description"):
        await w.upsert_series(
            series_code="exec.cmptn.opaque",
            source_id="kap",
            metric="m",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            description="Annual compensation for John A. Doe (CEO)",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_pseudonymous_series_with_pii_shaped_description_allowed(
    session: AsyncSession,
) -> None:
    """Guard only fires for pii_class='identifying'."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="company.revenue.q",
        source_id="kap",
        metric="revenue",
        frequency="1q",
        unit="TRY",
        pii_class="pseudonymous",
        description="Aselsan A.Ş. quarterly revenue",
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_opaque_code_and_no_description_allowed(
    session: AsyncSession,
) -> None:
    """An identifying series whose code/description are PII-free AND
    has at least one SubjectRef is allowed."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="exec.compensation.entity-aselsan.role-ceo.annual",
        source_id="kap",
        metric="compensation",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="kap-person:abc123", role="executive"),),
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_no_subjects_rejected(
    session: AsyncSession,
) -> None:
    """Codex F18: identifying series with empty subjects is structurally
    undeletable under Art. 17. Reject at the writer."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesMissingSubject, match="subject"):
        await w.upsert_series(
            series_code="exec.compensation.opaque",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            subjects=(),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_default_subjects_rejected(
    session: AsyncSession,
) -> None:
    """Codex F18: defaulted (omitted) subjects on identifying upsert
    must be treated identically to explicit empty."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesMissingSubject):
        await w.upsert_series(
            series_code="exec.compensation.opaque2",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_pseudonymous_series_with_no_subjects_allowed(
    session: AsyncSession,
) -> None:
    """The required-subject invariant ONLY fires for
    pii_class='identifying'."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="company.revenue.q.subj0",
        source_id="kap",
        metric="revenue",
        frequency="1q",
        unit="TRY",
        pii_class="pseudonymous",
        subjects=(),
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_pii_in_metadata_rejected(
    session: AsyncSession,
) -> None:
    """Codex F18: free-form metadata fields whose VALUES match
    PII-shape regexes are rejected on identifying upserts."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesMetadataPii, match="metadata"):
        await w.upsert_series(
            series_code="exec.compensation.opaque3",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            subjects=(SubjectRef(subject_id="kap-person:abc", role="executive"),),
            metadata={"notes": "contact john.doe@aselsan.com"},
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_series_with_clean_metadata_allowed(
    session: AsyncSession,
) -> None:
    """Sanity: identifying series with clean structured metadata + at
    least one subject is allowed."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="exec.compensation.opaque4",
        source_id="kap",
        metric="compensation",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="kap-person:xyz", role="executive"),),
        metadata={
            "subjects": [{"subject_id": "kap-person:xyz", "role": "executive"}],
            "fields": {"position": "CEO"},
        },
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_pii_in_metadata_fields_is_rejected(
    session: AsyncSession,
) -> None:
    """Codex F19: ``metadata.fields`` is NOT a free PII zone."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesMetadataPii, match="metadata"):
        await w.upsert_series(
            series_code="exec.compensation.opaque5",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            subjects=(SubjectRef(subject_id="kap-person:abc", role="executive"),),
            metadata={"fields": {"email": "a@b.c"}},
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_metadata_fields_with_non_pii_values_allowed(
    session: AsyncSession,
) -> None:
    """Codex F19: ``metadata.fields`` may carry domain data for
    identifying series."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="exec.compensation.opaque6",
        source_id="kap",
        metric="compensation",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="kap-person:abc", role="executive"),),
        metadata={
            "fields": {
                "compensation": 1_000_000,
                "role_label": "CEO",
                "period": "FY2025",
            },
        },
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_metadata_fields_recursively_scanned(
    session: AsyncSession,
) -> None:
    """Codex F19: PII nested inside dicts/lists under ``fields`` must
    also be rejected."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(IdentifyingSeriesMetadataPii, match="metadata"):
        await w.upsert_series(
            series_code="exec.compensation.opaque7",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            subjects=(SubjectRef(subject_id="kap-person:abc", role="executive"),),
            metadata={"fields": {"nested": {"email": "john.doe@example.com"}}},
        )


# ----- Codex F24 — forbid numeric-string object keys -----


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_with_numeric_string_key_at_top_level_rejected(
    session: AsyncSession,
) -> None:
    """Codex F24: object key whose string value parses as non-negative
    integer is forbidden."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        await w.upsert_series(
            series_code="company.revenue.f24a",
            source_id="kap",
            metric="revenue",
            frequency="1q",
            unit="TRY",
            pii_class="none",
            metadata={"0": "value"},
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_with_numeric_string_key_nested_rejected(
    session: AsyncSession,
) -> None:
    """Codex F24: validator is recursive."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        await w.upsert_series(
            series_code="company.revenue.f24b",
            source_id="kap",
            metric="revenue",
            frequency="1q",
            unit="TRY",
            pii_class="none",
            metadata={"fields": {"01": "y"}},
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_with_array_under_numeric_path_allowed(
    session: AsyncSession,
) -> None:
    """Codex F24: arrays of values are allowed — validator only rejects
    DICT keys that are numeric strings."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="company.revenue.f24c",
        source_id="kap",
        metric="revenue",
        frequency="1q",
        unit="TRY",
        pii_class="none",
        metadata={"fields": ["a", "b"]},
    )
    assert r.series_id > 0


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_with_numeric_string_key_rejected_for_identifying(
    session: AsyncSession,
) -> None:
    """Codex F24: validator runs for ALL pii_class values."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        await w.upsert_series(
            series_code="exec.compensation.f24d",
            source_id="kap",
            metric="compensation",
            frequency="1y",
            unit="TRY",
            pii_class="identifying",
            subjects=(SubjectRef(subject_id="kap-person:f24", role="executive"),),
            metadata={"42": "anything"},
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_metadata_with_non_numeric_keys_allowed(
    session: AsyncSession,
) -> None:
    """Codex F24: keys with a non-numeric prefix are the documented
    escape hatch."""
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="company.revenue.f24e",
        source_id="kap",
        metric="revenue",
        frequency="1q",
        unit="TRY",
        pii_class="none",
        metadata={"fields": {"item_0": "first", "_1": "second", "row_42": "third"}},
    )
    assert r.series_id > 0


# ----- Codex F20 — bypass detection by deletion-runtime helper -----


@pytest.mark.asyncio(loop_scope="session")
async def test_identifying_metadata_pii_inserted_via_direct_db_update_is_caught_by_deletion_scan(
    session: AsyncSession,
) -> None:
    """Codex F20: the writer's primary guard is bypassed by a direct-DB
    UPDATE; the deletion-runtime helper finds the leaked PII so the
    Art. 17 backstop in aslan-service can scrub it."""
    from aslan_core.timeseries.pii import find_pii_in_metadata

    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    r = await w.upsert_series(
        series_code="exec.compensation.opaque.f20",
        source_id="kap",
        metric="compensation",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="kap-person:f20", role="executive"),),
        metadata={"fields": {"role_label": "CEO"}},
    )
    await session.commit()

    # Simulate the bypass — direct UPDATE that smuggles PII into
    # metadata.fields. The writer would have rejected this; raw SQL
    # does not.
    await session.execute(
        text(
            "UPDATE ts.series_catalog "
            "SET metadata = jsonb_set(metadata, '{fields,email}', "
            "    '\"leak@example.com\"') "
            "WHERE series_id = :sid"
        ),
        {"sid": r.series_id},
    )
    await session.commit()

    leaked = await session.scalar(
        text("SELECT metadata FROM ts.series_catalog WHERE series_id = :sid"),
        {"sid": r.series_id},
    )
    findings = find_pii_in_metadata(leaked)
    assert len(findings) == 1
    assert findings[0].path == ("fields", "email")
    assert findings[0].json_path == "$.fields.email"
    assert findings[0].matched_pattern == "email"
    assert findings[0].matched_text == "leak@example.com"
    assert findings[0].full_value == "leak@example.com"
    assert findings[0].span == (0, len("leak@example.com"))
