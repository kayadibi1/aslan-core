from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import AuditRecord, current_actor
from aslan_core.audit import record as audit_record


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

        Audit semantics (codex F2 — target_table='watermark'):
          - success → watermark.advance event with before/after
          - CAS miss → no audit event (no mutation occurred)
        """
        ac = _audit_cols()
        if expected_cursor is None:
            res = cast(
                "CursorResult[tuple[object, ...]]",
                await self._s.execute(
                    text(
                        "INSERT INTO src.watermark "
                        "  (source_id, job_name, key, cursor_value, updated_at, "
                        "   actor_id, actor_kind, client_ip, user_agent, request_id) "
                        "VALUES (:sid, :job, :k, :new, now(), "
                        "        :actor_id, :actor_kind, :client_ip, "
                        "        :user_agent, :request_id) "
                        "ON CONFLICT (source_id, job_name, key) DO NOTHING"
                    ),
                    {
                        "sid": source_id,
                        "job": job_name,
                        "k": key,
                        "new": new_cursor,
                        **ac,
                    },
                ),
            )
            success = res.rowcount == 1
            if success:
                await audit_record(
                    self._s,
                    record=AuditRecord(
                        operation="watermark.advance",
                        target_schema="src",
                        target_table="watermark",
                        target_pk={
                            "source_id": source_id,
                            "job_name": job_name,
                            "key": key,
                        },
                        before=None,
                        after={
                            "source_id": source_id,
                            "job_name": job_name,
                            "key": key,
                            "cursor_value": new_cursor,
                        },
                        metadata={"path": "insert"},
                    ),
                )
            return success

        res = cast(
            "CursorResult[tuple[object, ...]]",
            await self._s.execute(
                text(
                    "UPDATE src.watermark "
                    "   SET cursor_value = :new, "
                    "       updated_at  = now(), "
                    "       actor_id    = :actor_id, "
                    "       actor_kind  = :actor_kind, "
                    "       client_ip   = :client_ip, "
                    "       user_agent  = :user_agent, "
                    "       request_id  = :request_id "
                    " WHERE source_id = :sid AND job_name = :job AND key = :k "
                    "   AND cursor_value = :expected"
                ),
                {
                    "sid": source_id,
                    "job": job_name,
                    "k": key,
                    "new": new_cursor,
                    "expected": expected_cursor,
                    **ac,
                },
            ),
        )
        success = res.rowcount == 1
        if success:
            await audit_record(
                self._s,
                record=AuditRecord(
                    operation="watermark.advance",
                    target_schema="src",
                    target_table="watermark",
                    target_pk={
                        "source_id": source_id,
                        "job_name": job_name,
                        "key": key,
                    },
                    before={
                        "source_id": source_id,
                        "job_name": job_name,
                        "key": key,
                        "cursor_value": expected_cursor,
                    },
                    after={
                        "source_id": source_id,
                        "job_name": job_name,
                        "key": key,
                        "cursor_value": new_cursor,
                    },
                    metadata={"path": "cas"},
                ),
            )
        return success

    async def set(
        self,
        source_id: str,
        job_name: str,
        key: str,
        cursor_value: str,
    ) -> None:
        """Unconditional UPSERT (repair / manual override).

        Will silently regress a cursor — caller's responsibility.

        Audit semantics: this is "force_set" for forensics — emits
        watermark.force_set with before/after. If the existing cursor
        already matched the new value AND no other field would
        change, emits watermark.idempotent_hit instead and skips the
        UPDATE so the row's audit cols stay frozen on the original
        writer (codex F1).
        """
        existing = (
            await self._s.execute(
                text(
                    "SELECT cursor_value FROM src.watermark "
                    "WHERE source_id = :sid AND job_name = :job AND key = :k"
                ),
                {"sid": source_id, "job": job_name, "k": key},
            )
        ).one_or_none()
        if existing is not None and existing.cursor_value == cursor_value:
            snap = {
                "source_id": source_id,
                "job_name": job_name,
                "key": key,
                "cursor_value": cursor_value,
            }
            await audit_record(
                self._s,
                record=AuditRecord(
                    operation="watermark.idempotent_hit",
                    target_schema="src",
                    target_table="watermark",
                    target_pk={
                        "source_id": source_id,
                        "job_name": job_name,
                        "key": key,
                    },
                    before=snap,
                    after=snap,
                    metadata={"returned_existing": True},
                ),
            )
            return

        ac = _audit_cols()
        await self._s.execute(
            text(
                "INSERT INTO src.watermark "
                "  (source_id, job_name, key, cursor_value, updated_at, "
                "   actor_id, actor_kind, client_ip, user_agent, request_id) "
                "VALUES (:sid, :job, :k, :v, now(), "
                "        :actor_id, :actor_kind, :client_ip, "
                "        :user_agent, :request_id) "
                "ON CONFLICT (source_id, job_name, key) DO UPDATE "
                "  SET cursor_value = EXCLUDED.cursor_value, "
                "      updated_at   = EXCLUDED.updated_at, "
                "      actor_id     = EXCLUDED.actor_id, "
                "      actor_kind   = EXCLUDED.actor_kind, "
                "      client_ip    = EXCLUDED.client_ip, "
                "      user_agent   = EXCLUDED.user_agent, "
                "      request_id   = EXCLUDED.request_id"
            ),
            {"sid": source_id, "job": job_name, "k": key, "v": cursor_value, **ac},
        )
        op = "watermark.set" if existing is None else "watermark.force_set"
        before = (
            None
            if existing is None
            else {
                "source_id": source_id,
                "job_name": job_name,
                "key": key,
                "cursor_value": existing.cursor_value,
            }
        )
        await audit_record(
            self._s,
            record=AuditRecord(
                operation=op,
                target_schema="src",
                target_table="watermark",
                target_pk={
                    "source_id": source_id,
                    "job_name": job_name,
                    "key": key,
                },
                before=before,
                after={
                    "source_id": source_id,
                    "job_name": job_name,
                    "key": key,
                    "cursor_value": cursor_value,
                },
                metadata={},
            ),
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


def _audit_cols() -> dict[str, Any]:
    """Build actor_id/actor_kind/client_ip/user_agent/request_id bind
    params for the current ContextVar actor (None-safe).

    Stamps src.watermark's denormalised audit cols inside the row
    INSERT/UPDATE so we never need a post-write second statement
    (codex F1).
    """
    a = current_actor()
    if a is None:
        return {
            "actor_id": None,
            "actor_kind": None,
            "client_ip": None,
            "user_agent": None,
            "request_id": None,
        }
    return {
        "actor_id": a.actor_id,
        "actor_kind": a.actor_kind,
        "client_ip": a.client_ip,
        "user_agent": a.user_agent,
        "request_id": a.request_id,
    }
