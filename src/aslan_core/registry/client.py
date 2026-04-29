from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text

from aslan_core.audit import AuditRecord, current_actor
from aslan_core.audit import record as audit_record
from aslan_core.errors import (
    EntityMergeRequired,
    EntityNotFound,
    IdentifierConflict,
    RegistryConstraintViolation,
)
from aslan_core.schemas.entity import Entity, EntityMatch


class EntityRegistryClient:
    def __init__(self, session: AsyncSession, ingestion_run_id: int) -> None:
        self._s = session
        self._run_id = ingestion_run_id
        self._source_id_cache: str | None = None

    # ─── Reads ───

    async def resolve(
        self,
        namespace: str,
        value: str,
        as_of: date | None = None,
    ) -> UUID | None:
        as_of = as_of or datetime.now(UTC).date()
        result: UUID | None = await self._s.scalar(
            text(
                "SELECT entity_id FROM ref.identifier "
                "WHERE namespace = :ns AND value = :v "
                "  AND :asof >= valid_from AND :asof < valid_to "
                "ORDER BY is_primary DESC, identifier_id DESC "
                "LIMIT 1"
            ),
            {"ns": namespace, "v": value, "asof": as_of},
        )
        return result

    async def resolve_or_raise(
        self,
        namespace: str,
        value: str,
        as_of: date | None = None,
    ) -> UUID:
        eid = await self.resolve(namespace, value, as_of)
        if eid is None:
            raise EntityNotFound(f"no entity for ({namespace}={value!r}, as_of={as_of})")
        return eid

    async def get(self, entity_id: UUID) -> Entity:
        row = (
            await self._s.execute(
                text(
                    "SELECT entity_id, entity_type, legal_name, short_name, country_code, "
                    "       domicile, incorporation_dt, status, fiscal_year_end, "
                    "       parent_entity_id, metadata, source_id, ingestion_run_id, "
                    "       created_at, updated_at "
                    "FROM ref.entity WHERE entity_id = :eid"
                ),
                {"eid": entity_id},
            )
        ).one_or_none()
        if row is None:
            raise EntityNotFound(str(entity_id))
        return self._row_to_entity(row)

    async def identifiers_for(
        self,
        entity_id: UUID,
        as_of: date | None = None,
    ) -> dict[str, str]:
        as_of = as_of or datetime.now(UTC).date()
        rows = (
            await self._s.execute(
                text(
                    "SELECT namespace, value FROM ref.identifier "
                    "WHERE entity_id = :eid "
                    "  AND :asof >= valid_from AND :asof < valid_to "
                    "ORDER BY is_primary DESC, identifier_id DESC"
                ),
                {"eid": entity_id, "asof": as_of},
            )
        ).all()
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r.namespace, r.value)
        return out

    async def search(
        self,
        query: str,
        types: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[EntityMatch]:
        statuses = statuses or ["active"]
        stmt = text(
            "SELECT "
            "    e.entity_id, e.entity_type, e.legal_name, e.short_name, "
            "    e.country_code, e.domicile, e.incorporation_dt, e.status, "
            "    e.fiscal_year_end, e.parent_entity_id, e.metadata, "
            "    e.source_id, e.ingestion_run_id, e.created_at, e.updated_at, "
            "    GREATEST( "
            "        similarity(e.legal_name, :q), "
            "        COALESCE(similarity(e.short_name, :q), 0) "
            "    ) AS sim "
            "FROM ref.entity e "
            "WHERE e.status = ANY(:statuses) "
            "  AND (:types IS NULL OR e.entity_type = ANY(:types)) "
            "  AND ( "
            "       e.legal_name % :q OR "
            "       (e.short_name IS NOT NULL AND e.short_name % :q) "
            "  ) "
            "ORDER BY sim DESC, e.legal_name ASC "
            "LIMIT :lim"
        ).bindparams(
            bindparam("statuses", type_=ARRAY(Text)),
            bindparam("types", type_=ARRAY(Text)),
        )
        rows = (
            await self._s.execute(
                stmt,
                {"q": query, "statuses": statuses, "types": types, "lim": limit},
            )
        ).all()
        out: list[EntityMatch] = []
        for r in rows:
            ent = self._row_to_entity(r)
            out.append(EntityMatch(entity=ent, matched_identifiers=[], similarity=float(r.sim)))
        return out

    # ─── Writes ───

    async def create_entity(
        self,
        *,
        type: str,  # noqa: A002 — public API mirrors Pydantic Entity.type
        legal_name: str,
        identifiers: dict[str, str],
        short_name: str | None = None,
        country_code: str = "TR",
        domicile: str | None = None,
        incorporation_dt: date | None = None,
        status: str = "active",
        fiscal_year_end: date | None = None,
        parent_entity_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        my_source = await self._source_id()

        # Look up existing identifier matches for any of the input pairs.
        rows: list[Any] = []
        if identifiers:
            pairs = list(identifiers.items())
            namespaces = [ns for ns, _ in pairs]
            values = [v for _, v in pairs]
            rows = list(
                (
                    await self._s.execute(
                        text(
                            "SELECT entity_id, namespace, value, source_id "
                            "FROM ref.identifier "
                            "WHERE (namespace, value) IN ( "
                            "  SELECT * FROM unnest(:nss, :vs) "
                            ") "
                            "  AND now()::date >= valid_from AND now()::date < valid_to"
                        ).bindparams(
                            bindparam("nss", type_=ARRAY(Text())),
                            bindparam("vs", type_=ARRAY(Text())),
                        ),
                        {"nss": namespaces, "vs": values},
                    )
                ).all()
            )

        match_eids = {r.entity_id for r in rows}

        if len(match_eids) > 1:
            raise EntityMergeRequired(
                f"identifiers map to {len(match_eids)} distinct entities: {match_eids}",
            )

        if match_eids:
            (target_eid,) = match_eids
            # The trust boundary is the target entity's source, not just the
            # identifier rows' source. Otherwise a source could attach its own
            # identifier to another source's entity via add_identifier and
            # then "merge" into it on the next create_entity call.
            target_entity = await self.get(target_eid)
            if target_entity.source_id != my_source:
                raise EntityMergeRequired(
                    f"target entity {target_eid} owned by "
                    f"source_id={target_entity.source_id}; current run is {my_source}",
                )
            for r in rows:
                if r.source_id != my_source:
                    raise EntityMergeRequired(
                        f"identifier ({r.namespace}={r.value}) was written by "
                        f"source_id={r.source_id}; current run is {my_source}",
                    )
            existing = {(r.namespace, r.value) for r in rows}
            for ns, val in identifiers.items():
                if (ns, val) not in existing:
                    await self.add_identifier(target_eid, ns, val)
            # Codex F1, 2026-04-29: do NOT mutate the existing row's audit
            # columns on an idempotent hit — the original creator's
            # attribution is preserved forever. Emit one
            # `entity.idempotent_hit` event with the current actor in the
            # event row so forensics still see "user X retried at time Z".
            await self._emit_audit(
                operation="entity.idempotent_hit",
                target_table="entity",
                target_pk={"entity_id": str(target_entity.entity_id)},
                before=target_entity.model_dump(mode="json"),
                after=target_entity.model_dump(mode="json"),
                metadata={"returned_existing": True},
            )
            return target_entity

        # Fresh INSERT — stamp audit columns IN the INSERT (single
        # round-trip). A separate post-write UPDATE would create a
        # rewrite-on-retry vulnerability per codex F1.
        ac = _audit_cols()
        eid: UUID = (
            await self._s.execute(
                text(
                    "INSERT INTO ref.entity "
                    "  (entity_type, legal_name, short_name, country_code, domicile, "
                    "   incorporation_dt, fiscal_year_end, status, parent_entity_id, "
                    "   metadata, source_id, ingestion_run_id, "
                    "   actor_id, actor_kind, client_ip, user_agent, request_id) "
                    "VALUES (:type, :ln, :sn, :cc, :dom, :inc, :fye, :status, :pid, "
                    "        COALESCE(:md, '{}')::jsonb, :sid, :run, "
                    "        :actor_id, :actor_kind, :client_ip, :user_agent, :request_id) "
                    "RETURNING entity_id"
                ),
                {
                    "type": type,
                    "ln": legal_name,
                    "sn": short_name,
                    "cc": country_code,
                    "dom": domicile,
                    "inc": incorporation_dt,
                    "fye": fiscal_year_end,
                    "status": status,
                    "pid": parent_entity_id,
                    "md": _jsonb(metadata),
                    "sid": my_source,
                    "run": self._run_id,
                    **ac,
                },
            )
        ).scalar_one()

        for ns, val in identifiers.items():
            await self.add_identifier(eid, ns, val, is_primary=False)

        new_entity = await self.get(eid)
        await self._emit_audit(
            operation="entity.create",
            target_table="entity",
            target_pk={"entity_id": str(new_entity.entity_id)},
            before=None,
            after=new_entity.model_dump(mode="json"),
        )
        return new_entity

    async def add_identifier(
        self,
        entity_id: UUID,
        namespace: str,
        value: str,
        valid_from: date | None = None,
        valid_to: date | None = None,
        is_primary: bool = False,
    ) -> None:
        """Idempotent on (namespace, value, valid_from). Collision with a
        different entity_id raises IdentifierConflict."""
        my_source = await self._source_id()
        existing = (
            await self._s.execute(
                text(
                    "SELECT entity_id, valid_from FROM ref.identifier "
                    "WHERE namespace = :ns AND value = :v "
                    "  AND valid_from = COALESCE(:vf, DATE '1900-01-01')"
                ),
                {"ns": namespace, "v": value, "vf": valid_from},
            )
        ).one_or_none()
        if existing is not None:
            if existing.entity_id != entity_id:
                raise IdentifierConflict(
                    f"({namespace}={value!r}, valid_from={existing.valid_from}) "
                    f"already maps to entity {existing.entity_id}",
                )
            return  # idempotent no-op

        try:
            await self._s.execute(
                text(
                    "INSERT INTO ref.identifier "
                    "  (entity_id, namespace, value, valid_from, valid_to, "
                    "   is_primary, source_id, ingestion_run_id) "
                    "VALUES (:eid, :ns, :v, "
                    "        COALESCE(:vf, DATE '1900-01-01'), "
                    "        COALESCE(:vt, DATE '9999-12-31'), "
                    "        :prim, :sid, :run)"
                ),
                {
                    "eid": entity_id,
                    "ns": namespace,
                    "v": value,
                    "vf": valid_from,
                    "vt": valid_to,
                    "prim": is_primary,
                    "sid": my_source,
                    "run": self._run_id,
                },
            )
            await self._s.flush()
        except Exception as e:
            # GiST exclusion violation = a *different* entity holds an
            # overlapping window for the same (namespace, value).
            raise IdentifierConflict(str(e)) from e

    async def update_entity(
        self,
        entity_id: UUID,
        *,
        legal_name: str | None = None,
        short_name: str | None = None,
        status: str | None = None,
        fiscal_year_end: date | None = None,
        domicile: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        """Partial update. Touches updated_at."""
        sets: list[str] = []
        params: dict[str, Any] = {"eid": entity_id}
        for field, val in (
            ("legal_name", legal_name),
            ("short_name", short_name),
            ("status", status),
            ("fiscal_year_end", fiscal_year_end),
            ("domicile", domicile),
        ):
            if val is not None:
                sets.append(f"{field} = :{field}")
                params[field] = val
        if metadata is not None:
            sets.append("metadata = metadata || :md::jsonb")
            params["md"] = _jsonb(metadata)
        sets.append("updated_at = now()")
        await self._s.execute(
            text(f"UPDATE ref.entity SET {', '.join(sets)} WHERE entity_id = :eid"),  # noqa: S608
            params,
        )
        return await self.get(entity_id)

    async def expire_identifier(
        self,
        namespace: str,
        value: str,
        as_of: date,
    ) -> None:
        """Set valid_to = as_of (FIRST INVALID DAY) on the active row."""
        res = cast(
            "CursorResult[Any]",
            await self._s.execute(
                text(
                    "UPDATE ref.identifier "
                    "   SET valid_to = :asof "
                    " WHERE namespace = :ns AND value = :v "
                    "   AND :asof >= valid_from AND :asof < valid_to"
                ),
                {"ns": namespace, "v": value, "asof": as_of},
            ),
        )
        if res.rowcount == 0:
            raise EntityNotFound(f"no active identifier ({namespace}={value!r})")

    async def link(
        self,
        parent_id: UUID,
        child_id: UUID,
        rel_type: str,
        weight: Decimal | None = None,
        valid_from: date | None = None,
        valid_to: date | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Idempotent on (parent_id, child_id, rel_type, valid_from).
        Existing rows update weight / valid_to / metadata."""
        my_source = await self._source_id()
        await self._s.execute(
            text(
                "INSERT INTO ref.entity_relationship "
                "  (parent_id, child_id, rel_type, weight, valid_from, valid_to, "
                "   metadata, source_id, ingestion_run_id) "
                "VALUES (:p, :c, :rt, :w, "
                "        COALESCE(:vf, DATE '1900-01-01'), "
                "        COALESCE(:vt, DATE '9999-12-31'), "
                "        COALESCE(:md, '{}')::jsonb, :sid, :run) "
                "ON CONFLICT (parent_id, child_id, rel_type, valid_from) "
                "  DO UPDATE SET weight   = EXCLUDED.weight, "
                "                valid_to = EXCLUDED.valid_to, "
                "                metadata = EXCLUDED.metadata"
            ),
            {
                "p": parent_id,
                "c": child_id,
                "rt": rel_type,
                "w": weight,
                "vf": valid_from,
                "vt": valid_to,
                "md": _jsonb(metadata),
                "sid": my_source,
                "run": self._run_id,
            },
        )

    async def upsert_sector(
        self,
        *,
        sector_id: str,
        taxonomy: str,
        code: str,
        name_tr: str,
        name_en: str | None = None,
        parent_sector_id: str | None = None,
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO ref.sector "
                "  (sector_id, taxonomy, code, name_tr, name_en, parent_sector_id) "
                "VALUES (:sid, :tax, :code, :ntr, :nen, :pid) "
                "ON CONFLICT (sector_id) DO UPDATE "
                "  SET taxonomy = EXCLUDED.taxonomy, "
                "      code = EXCLUDED.code, "
                "      name_tr = EXCLUDED.name_tr, "
                "      name_en = EXCLUDED.name_en, "
                "      parent_sector_id = EXCLUDED.parent_sector_id"
            ),
            {
                "sid": sector_id,
                "tax": taxonomy,
                "code": code,
                "ntr": name_tr,
                "nen": name_en,
                "pid": parent_sector_id,
            },
        )

    async def assign_sector(
        self,
        entity_id: UUID,
        sector_id: str,
        is_primary: bool = False,
        valid_from: date | None = None,
        valid_to: date | None = None,
    ) -> None:
        """Idempotent on (entity_id, sector_id, valid_from)."""
        await self._s.execute(
            text(
                "INSERT INTO ref.entity_sector "
                "  (entity_id, sector_id, is_primary, valid_from, valid_to) "
                "VALUES (:eid, :sid, :prim, "
                "        COALESCE(:vf, DATE '1900-01-01'), "
                "        COALESCE(:vt, DATE '9999-12-31')) "
                "ON CONFLICT (entity_id, sector_id, valid_from) "
                "  DO UPDATE SET is_primary = EXCLUDED.is_primary, "
                "                valid_to   = EXCLUDED.valid_to"
            ),
            {
                "eid": entity_id,
                "sid": sector_id,
                "prim": is_primary,
                "vf": valid_from,
                "vt": valid_to,
            },
        )

    # ─── helpers ───

    @staticmethod
    def _row_to_entity(row: Any) -> Entity:
        return Entity(
            entity_id=row.entity_id,
            type=row.entity_type,
            legal_name=row.legal_name,
            short_name=row.short_name,
            country_code=row.country_code,
            domicile=row.domicile,
            incorporation_dt=row.incorporation_dt,
            status=row.status,
            fiscal_year_end=row.fiscal_year_end,
            parent_entity_id=row.parent_entity_id,
            metadata=row.metadata or {},
            source_id=row.source_id,
            ingestion_run_id=row.ingestion_run_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _emit_audit(
        self,
        *,
        operation: str,
        target_table: str,
        target_pk: dict[str, Any],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit one audit event scoped to the registry (target_schema='ref').

        Centralised so every registry mutation writes events the same way
        and so the schema/run_id binding is not duplicated at every call
        site.
        """
        await audit_record(
            self._s,
            record=AuditRecord(
                operation=operation,
                target_schema="ref",
                target_table=target_table,
                target_pk=target_pk,
                before=before,
                after=after,
                metadata=metadata or {},
                ingestion_run_id=self._run_id,
            ),
        )

    async def _source_id(self) -> str:
        if self._source_id_cache is None:
            sid: str | None = await self._s.scalar(
                text("SELECT source_id FROM src.ingestion_run WHERE ingestion_run_id = :id"),
                {"id": self._run_id},
            )
            if sid is None:
                raise RegistryConstraintViolation(
                    f"ingestion_run_id={self._run_id} not found",
                )
            self._source_id_cache = sid
        return self._source_id_cache


def _jsonb(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d)


def _audit_cols() -> dict[str, Any]:
    """Build the actor_id/actor_kind/client_ip/user_agent/request_id bind
    params for the current ContextVar actor.

    When no actor is set, returns NULLs — the actual strict-mode raise
    happens inside ``audit.record()``. This helper is for *stamping the
    row's denormalised audit columns* at INSERT time so we never need a
    post-write UPDATE (which would let an idempotent retry rewrite the
    original creator's attribution per codex F1).
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
