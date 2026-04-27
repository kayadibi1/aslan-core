# aslan-core

The shared package every Aslan Terminal service imports. The package
contract lives in `../crawl/docs/aslan-core-spec.md`. The schema
contract lives in `../crawl/DATABASE_ARCHITECTURE.md`. Read both before
making structural changes.

## Hard rules

- ORM models in `aslan_core.models` are NOT public API. Public API is
  `registry`, `timeseries`, `documents`, `streams`, `ingestion`,
  `schemas`, `errors`, `cli`, `db.engine`, `db.session`.
- Migrations: one logical change per revision. Never edit an applied
  migration. File naming: `YYYYMMDD_HHMM_<short>.py`.
- Every public class / function has type hints. `mypy --strict` passes.
- Ruff format + lint clean. 100% line coverage on every public class.
- All datetimes tz-aware (UTC). Naive datetimes raise.
- Errors are typed (see `errors.py`). Never `except Exception:` in
  production paths.

## Build order

v0.1 → v0.2 → v0.3 → v1.0 per spec §12. Do NOT pull v0.2 work into
v0.1 ("while we're here"). Scope discipline.
