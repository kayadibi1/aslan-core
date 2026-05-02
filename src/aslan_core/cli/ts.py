"""``aslan ts`` Click group — operator-facing CLI for the v0.4.0
timeseries surface (spec §6).

Two subgroups:

* ``aslan ts series`` — upsert / get / list / stats over
  ``ts.series_catalog``.
* ``aslan ts observation`` — write (CSV) / latest / range over
  ``ts.observation``.

Auto-actor: every command inherits the ``cli:<user>@<host>`` Actor
set by the top-level ``aslan`` group (v0.3.0 Task 18). Audit rows
emitted by mutating commands carry that actor_id automatically.
Bulk write paths wrap the writer call in an ``ingestion_run``
context (mirroring ``aslan doc put``) so the FK on
``ts.observation.ingestion_run_id`` is satisfied and the run row
records the manual operation.
"""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import click
from sqlalchemy import text

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory, session_scope
from aslan_core.errors import (
    ObservationConflict,
    ObservationValidationError,
    SeriesNotFound,
)
from aslan_core.ingestion.run import ingestion_run
from aslan_core.schemas.timeseries import (
    Frequency,
    ObservationIn,
    PiiClass,
    RestatementBasis,
    SubjectRef,
    SubjectRole,
)
from aslan_core.timeseries import ObservationReader, ObservationWriter

# ─── Top-level group ─────────────────────────────────────────────────────


@click.group()
def ts() -> None:
    """Timeseries reads, queries, and manual writes."""


@ts.group("series")
def series_group() -> None:
    """``ts.series_catalog`` reads and mutations."""


@ts.group("observation")
def observation_group() -> None:
    """``ts.observation`` reads and bulk writes."""


# ─── Helpers ─────────────────────────────────────────────────────────────


def _json_or_human(payload: Any, json_flag: bool, *, human: str | None = None) -> None:
    """Mirror of ``aslan_core.cli.doc._json_or_human``: emit JSON when
    ``json_flag`` is set, otherwise the human-readable string."""
    if json_flag:
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(human if human is not None else str(payload))


def _parse_iso_tzaware(value: str, *, label: str) -> datetime:
    """Parse an ISO-8601 datetime; reject naive datetimes (UTC required).
    Mirrors ``aslan_core.cli.doc._parse_published_at`` so all tz-aware
    parsing in the CLI follows one rule: append ``Z`` or ``+00:00``."""
    dt = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.endswith("Z")
        else datetime.fromisoformat(value)
    )
    if dt.tzinfo is None:
        raise click.BadParameter(f"--{label} must be timezone-aware (UTC); append 'Z' or '+00:00'")
    return dt


_VALID_SUBJECT_ROLES: frozenset[str] = frozenset(
    {
        "data_subject",
        "reporter",
        "beneficial_owner",
        "insider",
        "executive",
        "board_member",
        "other",
    }
)


def _parse_subject(spec: str) -> SubjectRef:
    """Parse ``subject_id:role`` into a :class:`SubjectRef`. Raises
    :class:`click.BadParameter` on malformed input or unknown role."""
    if ":" not in spec:
        raise click.BadParameter(f"--subject must be 'subject_id:role'; got {spec!r}")
    subject_id, _, role = spec.partition(":")
    if not subject_id or not role:
        raise click.BadParameter(
            f"--subject must have a non-empty subject_id and role; got {spec!r}"
        )
    if role not in _VALID_SUBJECT_ROLES:
        raise click.BadParameter(f"--subject role {role!r} not in {sorted(_VALID_SUBJECT_ROLES)}")
    # mypy: role is narrowed by membership check above.
    typed_role: SubjectRole = role  # type: ignore[assignment]
    return SubjectRef(subject_id=subject_id, role=typed_role)


def _series_to_jsonable(series: Any) -> dict[str, Any]:
    """Convert a :class:`Series` Pydantic model to a JSON-serializable
    dict via ``model_dump(mode='json')``."""
    return dict(series.model_dump(mode="json"))


def _observation_to_jsonable(obs: Any) -> dict[str, Any]:
    return dict(obs.model_dump(mode="json"))


# ─── series upsert ────────────────────────────────────────────────────────


@series_group.command("upsert")
@click.option("--code", "series_code", required=True, help="Unique series_code.")
@click.option("--source-id", required=True, help="Must reference src.source.")
@click.option("--metric", required=True, help="Domain metric name (e.g. 'gdp', 'cpi').")
@click.option(
    "--frequency",
    required=True,
    type=click.Choice(
        [
            "tick",
            "1s",
            "1m",
            "5m",
            "15m",
            "30m",
            "1h",
            "1d",
            "1w",
            "1mo",
            "1q",
            "1y",
            "irregular",
        ]
    ),
    help="One of the spec Frequency literals.",
)
@click.option("--unit", required=True, help="Unit string (e.g. 'TRY', 'index', 'count').")
@click.option("--entity", "entity_id_str", default=None, help="UUID of the resolved entity.")
@click.option("--currency-code", default=None)
@click.option(
    "--restatement-basis",
    type=click.Choice(["nominal", "as_reported", "restated", "adjusted"]),
    default="nominal",
    show_default=True,
)
@click.option("--accounting-standard", default=None)
@click.option("--consolidation", default=None)
@click.option("--period-type", default=None)
@click.option("--description", default=None)
@click.option(
    "--pii-class",
    type=click.Choice(["none", "pseudonymous", "identifying"]),
    default="none",
    show_default=True,
)
@click.option(
    "--metadata",
    "metadata_json",
    default=None,
    help="JSON-encoded metadata dict.",
)
@click.option(
    "--subject",
    "subjects_raw",
    multiple=True,
    metavar="SUBJECT_ID:ROLE",
    help="Repeatable. Required for --pii-class identifying.",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def upsert_cmd(
    series_code: str,
    source_id: str,
    metric: str,
    frequency: str,
    unit: str,
    entity_id_str: str | None,
    currency_code: str | None,
    restatement_basis: str,
    accounting_standard: str | None,
    consolidation: str | None,
    period_type: str | None,
    description: str | None,
    pii_class: str,
    metadata_json: str | None,
    subjects_raw: tuple[str, ...],
    json_flag: bool,
) -> None:
    """Manual series upsert (operator path).

    Idempotent on ``series_code``: re-running with identical fields
    is a no-op (audit row tagged ``series.idempotent_hit``); changing
    a non-immutable field is an UPDATE (audit ``series.update``); any
    immutable-field change once observations exist raises
    ``SeriesCodeConflict`` and exits non-zero.
    """
    entity_id = UUID(entity_id_str) if entity_id_str else None
    metadata: dict[str, Any] | None = None
    if metadata_json is not None:
        try:
            parsed = json.loads(metadata_json)
        except json.JSONDecodeError as e:
            raise click.BadParameter(f"--metadata must be valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise click.BadParameter("--metadata must be a JSON object")
        metadata = parsed
    subjects = tuple(_parse_subject(s) for s in subjects_raw)
    # Cast string options to the Pydantic Literal types via runtime
    # validation in the writer; the @click.Choice decorators above
    # already bound the values, so the asserts here document intent
    # to mypy without runtime cost.
    typed_freq: Frequency = frequency  # type: ignore[assignment]
    typed_basis: RestatementBasis = restatement_basis  # type: ignore[assignment]
    typed_pii: PiiClass = pii_class  # type: ignore[assignment]
    asyncio.run(
        _upsert_series_impl(
            series_code=series_code,
            source_id=source_id,
            metric=metric,
            frequency=typed_freq,
            unit=unit,
            entity_id=entity_id,
            currency_code=currency_code,
            restatement_basis=typed_basis,
            accounting_standard=accounting_standard,
            consolidation=consolidation,
            period_type=period_type,
            description=description,
            pii_class=typed_pii,
            metadata=metadata,
            subjects=subjects,
            json_flag=json_flag,
        )
    )


async def _upsert_series_impl(
    *,
    series_code: str,
    source_id: str,
    metric: str,
    frequency: Frequency,
    unit: str,
    entity_id: UUID | None,
    currency_code: str | None,
    restatement_basis: RestatementBasis,
    accounting_standard: str | None,
    consolidation: str | None,
    period_type: str | None,
    description: str | None,
    pii_class: PiiClass,
    metadata: dict[str, Any] | None,
    subjects: tuple[SubjectRef, ...],
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        # Open an ingestion_run so the writer's audit + (later) any
        # observations link to a real run row. job_name marks this as
        # an operator path so log scrapers can filter manual ops.
        async with (
            ingestion_run(engine, source_id=source_id, job_name="cli.ts.series_upsert") as run,
            session_scope(factory) as s,
        ):
            writer = ObservationWriter(s, ingestion_run_id=run.id)
            result = await writer.upsert_series(
                series_code=series_code,
                source_id=source_id,
                metric=metric,
                frequency=frequency,
                unit=unit,
                entity_id=entity_id,
                currency_code=currency_code,
                restatement_basis=restatement_basis,
                accounting_standard=accounting_standard,
                consolidation=consolidation,
                period_type=period_type,
                description=description,
                pii_class=pii_class,
                metadata=metadata,
                subjects=subjects,
            )
        payload = {
            "series_id": result.series_id,
            "series_code": series_code,
            "created": result.created,
        }
        _json_or_human(
            payload,
            json_flag,
            human=f"{result.series_id}\t{series_code}\tcreated={result.created}",
        )
    finally:
        await engine.dispose()


# ─── series get ──────────────────────────────────────────────────────────


@series_group.command("get")
@click.argument("series_code")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def get_cmd(series_code: str, json_flag: bool) -> None:
    """Fetch a single series by ``series_code``. Prints ``null`` (JSON)
    or ``(no match)`` (human) when the code is unknown — exit code is
    ``0`` either way; absence is not an error."""
    asyncio.run(_get_series_impl(series_code, json_flag))


async def _get_series_impl(series_code: str, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            reader = ObservationReader(s)
            series = await reader.get_series(series_code)
        if series is None:
            if json_flag:
                click.echo(json.dumps(None))
            else:
                click.echo("(no match)")
            return
        payload = _series_to_jsonable(series)
        if json_flag:
            click.echo(json.dumps(payload, default=str))
        else:
            click.echo(
                f"{series.series_id}\t{series.series_code}\t{series.source_id}\t"
                f"{series.metric}\t{series.frequency}\t{series.unit}"
            )
    finally:
        await engine.dispose()


# ─── series list ─────────────────────────────────────────────────────────


@series_group.command("list")
@click.option("--source-id", default=None, help="Filter by source_id.")
@click.option("--metric", default=None, help="Filter by metric.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def list_cmd(
    source_id: str | None,
    metric: str | None,
    limit: int,
    json_flag: bool,
) -> None:
    """List series with optional source-id / metric filters."""
    asyncio.run(_list_series_impl(source_id, metric, limit, json_flag))


async def _list_series_impl(
    source_id: str | None,
    metric: str | None,
    limit: int,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sql = (
                "SELECT series_id, series_code, source_id, metric, frequency, "
                "       unit, pii_class, created_at "
                "FROM ts.series_catalog WHERE 1=1"
            )
            params: dict[str, Any] = {}
            if source_id is not None:
                sql += " AND source_id = :sid"
                params["sid"] = source_id
            if metric is not None:
                sql += " AND metric = :metric"
                params["metric"] = metric
            sql += " ORDER BY series_code LIMIT :limit"
            params["limit"] = limit
            rows = (await s.execute(text(sql), params)).mappings().all()
        payload = [
            {
                "series_id": r["series_id"],
                "series_code": r["series_code"],
                "source_id": r["source_id"],
                "metric": r["metric"],
                "frequency": r["frequency"],
                "unit": r["unit"],
                "pii_class": r["pii_class"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            for r in payload:
                click.echo(
                    f"{r['series_id']}\t{r['series_code']}\t{r['source_id']}\t"
                    f"{r['metric']}\t{r['frequency']}\t{r['unit']}"
                )
    finally:
        await engine.dispose()


# ─── series stats ────────────────────────────────────────────────────────


@series_group.command("stats")
@click.option("--source-id", default=None, help="Filter by source_id.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def stats_cmd(source_id: str | None, json_flag: bool) -> None:
    """Counts grouped by ``(source_id, frequency)``."""
    asyncio.run(_stats_series_impl(source_id, json_flag))


async def _stats_series_impl(source_id: str | None, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sql = "SELECT source_id, frequency, COUNT(*) AS cnt FROM ts.series_catalog"
            params: dict[str, Any] = {}
            if source_id is not None:
                sql += " WHERE source_id = :sid"
                params["sid"] = source_id
            sql += " GROUP BY source_id, frequency ORDER BY source_id, frequency"
            rows = (await s.execute(text(sql), params)).mappings().all()
        payload = [
            {
                "source_id": r["source_id"],
                "frequency": r["frequency"],
                "count": int(r["cnt"]),
            }
            for r in rows
        ]
        if json_flag:
            click.echo(json.dumps(payload))
        else:
            for r in payload:
                click.echo(f"{r['source_id']}\t{r['frequency']}\t{r['count']}")
    finally:
        await engine.dispose()


# ─── observation write (CSV) ─────────────────────────────────────────────


@observation_group.command("write")
@click.option("--series-code", required=True, help="Target series_code.")
@click.option(
    "--csv",
    "csv_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a CSV file with one ObservationIn per row.",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def write_cmd(series_code: str, csv_path: str, json_flag: bool) -> None:
    """Bulk-write observations from a CSV.

    Required columns:

    * ``ts`` — ISO-8601 tz-aware (append ``Z`` or ``+00:00``)
    * ``as_of`` — ISO-8601 tz-aware
    * ``value`` OR ``value_text`` — exactly one per row

    Optional columns:

    * ``quality_flag`` (default ``0``)
    * ``metadata`` — JSON-encoded dict

    A row with both ``value`` and ``value_text`` populated raises
    ``ObservationValidationError`` and exits non-zero. Naive datetimes
    are rejected at parse time.
    """
    rows = list(_read_csv_observations(Path(csv_path)))
    asyncio.run(_observation_write_impl(series_code, rows, json_flag))


def _read_csv_observations(path: Path) -> list[ObservationIn]:
    """Parse a CSV into a list of :class:`ObservationIn`. Raises
    :class:`click.BadParameter` on naive datetimes or when a row has
    both / neither ``value`` and ``value_text``. The actual Pydantic
    validators run on construction; the surface here translates DB-y
    errors into operator-friendly messages."""
    out: list[ObservationIn] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise click.BadParameter(f"CSV {path} has no header row")
        for i, row in enumerate(reader):
            ts_raw = (row.get("ts") or "").strip()
            as_of_raw = (row.get("as_of") or "").strip()
            if not ts_raw:
                raise click.BadParameter(f"CSV row {i + 2}: missing ts")
            if not as_of_raw:
                raise click.BadParameter(f"CSV row {i + 2}: missing as_of")
            ts = _parse_iso_tzaware(ts_raw, label=f"csv[{i + 2}].ts")
            as_of = _parse_iso_tzaware(as_of_raw, label=f"csv[{i + 2}].as_of")

            value_raw = (row.get("value") or "").strip()
            value_text_raw = (row.get("value_text") or "").strip()
            value: float | None = None
            value_text: str | None = None
            if value_raw and value_text_raw:
                raise click.BadParameter(
                    f"CSV row {i + 2}: must have exactly one of value or value_text"
                )
            if value_raw:
                try:
                    value = float(value_raw)
                except ValueError as e:
                    raise click.BadParameter(
                        f"CSV row {i + 2}: value {value_raw!r} not a float"
                    ) from e
            elif value_text_raw:
                value_text = value_text_raw
            else:
                raise click.BadParameter(f"CSV row {i + 2}: must provide value or value_text")

            qf_raw = (row.get("quality_flag") or "").strip()
            quality_flag = int(qf_raw) if qf_raw else 0

            meta_raw = (row.get("metadata") or "").strip()
            metadata: dict[str, Any] = {}
            if meta_raw:
                try:
                    parsed = json.loads(meta_raw)
                except json.JSONDecodeError as e:
                    raise click.BadParameter(
                        f"CSV row {i + 2}: metadata is not valid JSON: {e}"
                    ) from e
                if not isinstance(parsed, dict):
                    raise click.BadParameter(f"CSV row {i + 2}: metadata must be a JSON object")
                metadata = parsed

            out.append(
                ObservationIn(
                    ts=ts,
                    as_of=as_of,
                    value=value,
                    value_text=value_text,
                    quality_flag=quality_flag,
                    metadata=metadata,
                )
            )
    return out


async def _observation_write_impl(
    series_code: str,
    observations: list[ObservationIn],
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        # Resolve series_code → series_id BEFORE opening the run so a
        # bogus code does not leak an empty 'cli.ts.observation_write'
        # run row. Use a short-lived session for the lookup so the
        # writer's session is fresh when we open the run.
        async with session_scope(factory) as s:
            sid_raw = await s.scalar(
                text("SELECT series_id FROM ts.series_catalog WHERE series_code = :code"),
                {"code": series_code},
            )
            source_id_raw = await s.scalar(
                text("SELECT source_id FROM ts.series_catalog WHERE series_code = :code"),
                {"code": series_code},
            )
        if sid_raw is None or source_id_raw is None:
            raise SeriesNotFound(f"series_code={series_code!r} not found")
        series_id = int(sid_raw)
        source_id = str(source_id_raw)

        async with (
            ingestion_run(engine, source_id=source_id, job_name="cli.ts.observation_write") as run,
            session_scope(factory) as s,
        ):
            writer = ObservationWriter(s, ingestion_run_id=run.id)
            try:
                result = await writer.write(series_id, observations)
            except (ObservationValidationError, ObservationConflict) as e:
                raise click.ClickException(str(e)) from e
            await run.increment_rows(result.attempted)
        payload = result.model_dump(mode="json")
        _json_or_human(
            payload,
            json_flag,
            human=(
                f"attempted={result.attempted}\tinserted={result.inserted}\t"
                f"unchanged={result.unchanged}"
            ),
        )
    finally:
        await engine.dispose()


# ─── observation latest ──────────────────────────────────────────────────


@observation_group.command("latest")
@click.argument("series_code")
@click.option(
    "--as-of",
    "as_of_str",
    default=None,
    metavar="ISO-8601",
    help="Point-in-time filter (tz-aware).",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def latest_cmd(series_code: str, as_of_str: str | None, json_flag: bool) -> None:
    """Fetch the most-recent observation for ``series_code``, with
    optional PIT collapse on ``--as-of``."""
    as_of = _parse_iso_tzaware(as_of_str, label="as-of") if as_of_str else None
    asyncio.run(_latest_impl(series_code, as_of, json_flag))


async def _latest_impl(
    series_code: str,
    as_of: datetime | None,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sid_raw = await s.scalar(
                text("SELECT series_id FROM ts.series_catalog WHERE series_code = :code"),
                {"code": series_code},
            )
            if sid_raw is None:
                raise SeriesNotFound(f"series_code={series_code!r} not found")
            reader = ObservationReader(s)
            obs = await reader.latest(int(sid_raw), as_of=as_of)
        if obs is None:
            if json_flag:
                click.echo(json.dumps(None))
            else:
                click.echo("(no observations)")
            return
        payload = _observation_to_jsonable(obs)
        if json_flag:
            click.echo(json.dumps(payload, default=str))
        else:
            click.echo(
                f"{obs.ts.isoformat()}\t{obs.as_of.isoformat()}\t"
                f"value={obs.value}\tvalue_text={obs.value_text}"
            )
    finally:
        await engine.dispose()


# ─── observation range ───────────────────────────────────────────────────


@observation_group.command("range")
@click.argument("series_code")
@click.option(
    "--start",
    "ts_start_str",
    required=True,
    metavar="ISO-8601",
    help="Inclusive lower bound on ts (tz-aware).",
)
@click.option(
    "--end",
    "ts_end_str",
    required=True,
    metavar="ISO-8601",
    help="Exclusive upper bound on ts (tz-aware).",
)
@click.option(
    "--as-of",
    "as_of_str",
    default=None,
    metavar="ISO-8601",
    help="Point-in-time filter (tz-aware).",
)
@click.option("--limit", type=int, default=None)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def range_cmd(
    series_code: str,
    ts_start_str: str,
    ts_end_str: str,
    as_of_str: str | None,
    limit: int | None,
    json_flag: bool,
) -> None:
    """Half-open ``[--start, --end)`` range with optional PIT collapse."""
    ts_start = _parse_iso_tzaware(ts_start_str, label="start")
    ts_end = _parse_iso_tzaware(ts_end_str, label="end")
    as_of = _parse_iso_tzaware(as_of_str, label="as-of") if as_of_str else None
    asyncio.run(_range_impl(series_code, ts_start, ts_end, as_of, limit, json_flag))


async def _range_impl(
    series_code: str,
    ts_start: datetime,
    ts_end: datetime,
    as_of: datetime | None,
    limit: int | None,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sid_raw = await s.scalar(
                text("SELECT series_id FROM ts.series_catalog WHERE series_code = :code"),
                {"code": series_code},
            )
            if sid_raw is None:
                raise SeriesNotFound(f"series_code={series_code!r} not found")
            reader = ObservationReader(s)
            obs_list = await reader.range(
                int(sid_raw),
                ts_start,
                ts_end,
                as_of=as_of,
                limit=limit,
            )
        payload = [_observation_to_jsonable(o) for o in obs_list]
        if json_flag:
            click.echo(json.dumps(payload, default=str))
        else:
            for o in obs_list:
                click.echo(
                    f"{o.ts.isoformat()}\t{o.as_of.isoformat()}\t"
                    f"value={o.value}\tvalue_text={o.value_text}"
                )
    finally:
        await engine.dispose()
