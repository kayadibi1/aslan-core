from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text

from aslan_core.errors import (
    EntityNotFound,
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
