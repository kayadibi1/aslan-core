from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession


class WatermarkStore:
    """Per-(source, job, key) cursor store with CAS semantics.

    Spec §5.2. Cursors are opaque strings — interpretation is the
    caller's responsibility. The CAS-style `advance()` is the safe path;
    `set()` is for repair / manual override and will silently regress.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(
        self,
        source_id: str,
        job_name: str,
        key: str,
    ) -> str | None:
        result: str | None = await self._s.scalar(
            text(
                "SELECT cursor_value FROM src.watermark "
                "WHERE source_id = :sid AND job_name = :job AND key = :k"
            ),
            {"sid": source_id, "job": job_name, "k": key},
        )
        return result

    async def advance(
        self,
        source_id: str,
        job_name: str,
        key: str,
        new_cursor: str,
        expected_cursor: str | None,
    ) -> bool:
        """CAS UPDATE. Returns True on success, False on miss.

        When `expected_cursor is None`, this is the insert-only path:
        succeeds iff no row exists yet (INSERT ... ON CONFLICT DO NOTHING).
        When `expected_cursor` is a value, this is a conditional UPDATE
        guarded by `cursor_value = :expected`; rowcount is 0 if no row
        exists OR the existing cursor doesn't match.
        """
        if expected_cursor is None:
            res = cast(
                "CursorResult[tuple[object, ...]]",
                await self._s.execute(
                    text(
                        "INSERT INTO src.watermark "
                        "  (source_id, job_name, key, cursor_value, updated_at) "
                        "VALUES (:sid, :job, :k, :new, now()) "
                        "ON CONFLICT (source_id, job_name, key) DO NOTHING"
                    ),
                    {"sid": source_id, "job": job_name, "k": key, "new": new_cursor},
                ),
            )
            return res.rowcount == 1

        res = cast(
            "CursorResult[tuple[object, ...]]",
            await self._s.execute(
                text(
                    "UPDATE src.watermark "
                    "   SET cursor_value = :new, updated_at = now() "
                    " WHERE source_id = :sid AND job_name = :job AND key = :k "
                    "   AND cursor_value = :expected"
                ),
                {
                    "sid": source_id,
                    "job": job_name,
                    "k": key,
                    "new": new_cursor,
                    "expected": expected_cursor,
                },
            ),
        )
        return res.rowcount == 1

    async def set(
        self,
        source_id: str,
        job_name: str,
        key: str,
        cursor_value: str,
    ) -> None:
        """Unconditional UPSERT (repair / manual override).

        Will silently regress a cursor — caller's responsibility.
        """
        await self._s.execute(
            text(
                "INSERT INTO src.watermark "
                "  (source_id, job_name, key, cursor_value, updated_at) "
                "VALUES (:sid, :job, :k, :v, now()) "
                "ON CONFLICT (source_id, job_name, key) DO UPDATE "
                "  SET cursor_value = EXCLUDED.cursor_value, "
                "      updated_at   = EXCLUDED.updated_at"
            ),
            {"sid": source_id, "job": job_name, "k": key, "v": cursor_value},
        )

    async def list_for_job(
        self,
        source_id: str,
        job_name: str,
    ) -> dict[str, str]:
        rows = (
            await self._s.execute(
                text(
                    "SELECT key, cursor_value FROM src.watermark "
                    "WHERE source_id = :sid AND job_name = :job"
                ),
                {"sid": source_id, "job": job_name},
            )
        ).all()
        return {r.key: r.cursor_value for r in rows}
