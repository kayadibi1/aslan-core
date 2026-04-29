from __future__ import annotations

from typing import Any, Protocol, TypedDict

import aioboto3


class ObjectMetadata(TypedDict):
    bytes: int
    content_type: str


class ObjectStorageClient(Protocol):
    """PEP-544 Protocol for S3-compatible object storage.

    Implementations: Aioboto3ObjectStorageClient (default, wraps aioboto3
    against MinIO/R2) and InMemoryFake (canonical test fake).
    """

    async def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> None: ...

    async def head_object(self, *, bucket: str, key: str) -> ObjectMetadata | None: ...

    async def delete_object(self, *, bucket: str, key: str) -> None: ...

    async def presigned_url(self, *, bucket: str, key: str, expires_in: int = 3600) -> str: ...


class InMemoryFake:
    """Test-only fake implementing ObjectStorageClient.

    Stores bodies + metadata in dicts keyed by (bucket, key). Tests can
    use ``all_keys()`` and ``get_body()`` for assertions.
    """

    def __init__(self) -> None:
        self._bodies: dict[tuple[str, str], bytes] = {}
        self._meta: dict[tuple[str, str], ObjectMetadata] = {}

    async def put_object(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self._bodies[(bucket, key)] = body
        self._meta[(bucket, key)] = ObjectMetadata(bytes=len(body), content_type=content_type)

    async def head_object(self, *, bucket: str, key: str) -> ObjectMetadata | None:
        return self._meta.get((bucket, key))

    async def delete_object(self, *, bucket: str, key: str) -> None:
        # Idempotent — no error on missing key (matches S3 semantics)
        self._bodies.pop((bucket, key), None)
        self._meta.pop((bucket, key), None)

    async def presigned_url(self, *, bucket: str, key: str, expires_in: int = 3600) -> str:
        return f"https://fake.local/{bucket}/{key}?expires={expires_in}"

    # Test helpers (NOT part of the Protocol)

    def all_keys(self) -> set[tuple[str, str]]:
        return set(self._meta.keys())

    def get_body(self, bucket: str, key: str) -> bytes | None:
        return self._bodies.get((bucket, key))


class Aioboto3ObjectStorageClient:
    """Default ObjectStorageClient impl wrapping aioboto3 (S3 / MinIO / R2).

    All AWS-style S3-compatible stores work: AWS S3, MinIO, Cloudflare R2,
    SeaweedFS, etc. Endpoint URL controls which.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._endpoint_url = endpoint_url

    def _client(self) -> Any:
        # Each call returns a fresh async-context-manager-managed client.
        # Caller uses ``async with``.
        return self._session.client("s3", endpoint_url=self._endpoint_url)

    async def put_object(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )

    async def head_object(self, *, bucket: str, key: str) -> ObjectMetadata | None:
        async with self._client() as s3:
            try:
                resp = await s3.head_object(Bucket=bucket, Key=key)
            except s3.exceptions.ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
        return ObjectMetadata(
            bytes=resp["ContentLength"],
            content_type=resp.get("ContentType", "application/octet-stream"),
        )

    async def delete_object(self, *, bucket: str, key: str) -> None:
        async with self._client() as s3:
            try:
                await s3.delete_object(Bucket=bucket, Key=key)
            except s3.exceptions.ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                    return
                raise

    async def presigned_url(self, *, bucket: str, key: str, expires_in: int = 3600) -> str:
        async with self._client() as s3:
            url: str = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url
