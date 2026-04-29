from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_filing_construction() -> None:
    from aslan_core.schemas.filing import Filing

    f = Filing(
        filing_id=uuid4(),
        source_id="kap",
        source_filing_ref="DISC-12345",
        entity_id=uuid4(),
        kind="material_event",
        subkind=None,
        title="Test disclosure",
        language="tr",
        published_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        period_start=None,
        period_end=None,
        source_url=None,
        is_amendment=False,
        previous_filing_id=None,
        primary_object_key="kap/x/2026/04/28/y/main.pdf",
        primary_mime="application/pdf",
        primary_sha256="a" * 64,
        primary_bytes=1234,
        has_xbrl=False,
        xbrl_object_key=None,
        metadata={},
        discovered_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        revision_no=1,
    )
    assert f.revision_no == 1
    assert f.is_amendment is False


def test_filing_rejects_naive_datetime() -> None:
    """Per CLAUDE.md: tz-aware datetimes only."""
    from aslan_core.schemas.filing import Filing

    with pytest.raises(ValidationError):
        Filing(
            filing_id=uuid4(),
            source_id="kap",
            source_filing_ref="X",
            entity_id=None,
            kind="news",
            subkind=None,
            title="t",
            language="tr",
            published_at=datetime(2026, 4, 28, 12, 0),  # noqa: DTZ001  # naive — should fail
            period_start=None,
            period_end=None,
            source_url=None,
            is_amendment=False,
            previous_filing_id=None,
            primary_object_key="k",
            primary_mime="text/html",
            primary_sha256="b" * 64,
            primary_bytes=1,
            has_xbrl=False,
            xbrl_object_key=None,
            metadata={},
            discovered_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
            revision_no=1,
        )


def test_attachment_in_construction() -> None:
    from aslan_core.schemas.filing import AttachmentIn

    a = AttachmentIn(
        bytes=b"hello",
        mime="text/plain",
        filename="hello.txt",
        role="exhibit",
        sequence=1,
    )
    assert a.role == "exhibit"


def test_put_filing_result_carries_manifest() -> None:
    """codex 2026-04-28 review: result.object_keys + result.bucket are
    the source of truth for release(). Verify they're present and typed."""
    from aslan_core.schemas.filing import Filing, PutFilingResult

    f = Filing(
        filing_id=uuid4(),
        source_id="kap",
        source_filing_ref="X",
        entity_id=None,
        kind="news",
        subkind=None,
        title="t",
        language="tr",
        published_at=datetime(2026, 4, 28, tzinfo=UTC),
        period_start=None,
        period_end=None,
        source_url=None,
        is_amendment=False,
        previous_filing_id=None,
        primary_object_key="k",
        primary_mime="text/html",
        primary_sha256="c" * 64,
        primary_bytes=1,
        has_xbrl=False,
        xbrl_object_key=None,
        metadata={},
        discovered_at=datetime(2026, 4, 28, tzinfo=UTC),
        revision_no=1,
    )
    r = PutFilingResult(
        filing=f,
        created=True,
        is_revision=False,
        revision_no=1,
        bucket="aslan-filings",
        object_keys=["kap/x/2026/04/28/y/main.html", "kap/x/2026/04/28/y/exh1.pdf"],
    )
    assert r.created is True
    assert r.bucket == "aslan-filings"
    assert len(r.object_keys) == 2
