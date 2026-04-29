from __future__ import annotations

import pytest

from aslan_core.documents.object_storage import InMemoryFake


@pytest.mark.asyncio
async def test_put_then_head_returns_metadata() -> None:
    store = InMemoryFake()
    await store.put_object(
        bucket="aslan-filings",
        key="kap/abc/2026/04/28/xyz/main.pdf",
        body=b"hello world",
        content_type="application/pdf",
    )
    head = await store.head_object(bucket="aslan-filings", key="kap/abc/2026/04/28/xyz/main.pdf")
    assert head is not None
    assert head["bytes"] == 11
    assert head["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_head_returns_none_for_missing_key() -> None:
    store = InMemoryFake()
    head = await store.head_object(bucket="aslan-filings", key="missing")
    assert head is None


@pytest.mark.asyncio
async def test_delete_object_removes_key() -> None:
    store = InMemoryFake()
    await store.put_object(bucket="b", key="k", body=b"x", content_type="text/plain")
    await store.delete_object(bucket="b", key="k")
    assert await store.head_object(bucket="b", key="k") is None


@pytest.mark.asyncio
async def test_delete_missing_key_is_idempotent_noop() -> None:
    store = InMemoryFake()
    # Should not raise
    await store.delete_object(bucket="b", key="missing")
