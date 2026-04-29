from __future__ import annotations

import contextlib

import pytest
from testcontainers.minio import MinioContainer

from aslan_core.documents.object_storage import Aioboto3ObjectStorageClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def minio():  # type: ignore[no-untyped-def]
    with MinioContainer("minio/minio:latest") as mc:
        yield mc


@pytest.fixture(scope="session")
def s3_endpoint(minio: MinioContainer) -> str:
    return f"http://{minio.get_container_host_ip()}:{minio.get_exposed_port(minio.port)}"


@pytest.fixture(scope="session")
async def bucket_name(minio: MinioContainer, s3_endpoint: str) -> str:
    """Pre-create the test bucket. Session-scoped so all tests in this
    file share it."""
    import aioboto3

    session = aioboto3.Session(
        aws_access_key_id=minio.access_key,
        aws_secret_access_key=minio.secret_key,
    )
    async with session.client("s3", endpoint_url=s3_endpoint) as s3:
        with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
            await s3.create_bucket(Bucket="aslan-test-filings")
    return "aslan-test-filings"


@pytest.mark.asyncio(loop_scope="session")
async def test_aioboto3_put_head_get_delete_round_trip(
    minio: MinioContainer, s3_endpoint: str, bucket_name: str
) -> None:
    client = Aioboto3ObjectStorageClient(
        endpoint_url=s3_endpoint,
        access_key=minio.access_key,
        secret_key=minio.secret_key,
        region="us-east-1",
    )
    body = b"hello minio"
    await client.put_object(
        bucket=bucket_name,
        key="test/key.bin",
        body=body,
        content_type="application/octet-stream",
    )
    head = await client.head_object(bucket=bucket_name, key="test/key.bin")
    assert head is not None
    assert head["bytes"] == len(body)

    await client.delete_object(bucket=bucket_name, key="test/key.bin")
    assert await client.head_object(bucket=bucket_name, key="test/key.bin") is None
