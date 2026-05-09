# Bitemporal best-practices research — Aslan Terminal Research API

Scope: a survey of bitemporal database literature, standards, and product
implementations to ground the upcoming `aslan-core` point-in-time research
API. Hard constraint: **Postgres 16 only**. Per ADR-002, every non-Postgres
runtime (XTDB, Datomic, QLDB, MariaDB, SAP HANA, SQL Server) is treated as
*concept-only*. Their primitives are not on the table; their semantics are.

This document does not propose schemas (that is the job of `SCOPE.md`). It
catalogues the patterns, conflicts, and trade-offs the schema work will sit
on top of.

---

## 1. Bitemporal terminology (consensus glossary)

### 1.1 Valid time, transaction time, as-of

The Jensen / Dyreson et al. **Consensus Glossary of Temporal Database
Concepts** (1998, building on the 1992 / 1994 SIGMOD Record versions) is the
authoritative source for terminology and is the ancestor of every modern
treatment. The relevant definitions:

- **Valid time** of a fact: the time(s) at which the fact is *true in the
  modelled reality*. May be in the past, present, or future relative to
  the database; may be a point or an interval.
- **Transaction time** of a fact: the time(s) at which the fact is
  *current in the database* — i.e. the period from when the row was
  inserted to when it was logically superseded. Transaction time
  cannot be in the future and cannot be retroactively rewritten without
  destroying the audit trail.
- **Bitemporal relation**: a relation that captures one or more valid
  times **and** one or more transaction times for each tuple. The "bi"
  refers strictly to two temporal aspects, not two columns.
- (Glossary, Jensen et al., SIGMOD Record 1994 / 1998 update —
  <https://www2.cs.arizona.edu/~rts/pubs/SIGMODRecordMarch94p52.pdf>;
  PDF mirror at <https://infolab.usc.edu/csci599/Fall2001/paper/glossary.pdf>.)

Snodgrass's *Developing Time-Oriented Database Applications in SQL* (Morgan
Kaufmann, 1999; free PDF on <http://www2.cs.arizona.edu/~rts/publications.html>)
canonicalised valid-time vs transaction-time for the practitioner audience
and provided the term **bitemporal table** as a table that records both.
The Wikipedia summary (<https://en.wikipedia.org/wiki/Temporal_database>)
correctly notes that the SQL:2011 standard adopted Snodgrass's terminology.

**"As-of"** is a query predicate, not a stored column. In Snodgrass's
terms, an as-of query asks "what did the database believe at transaction
time *T*?" — it slices the bitemporal cube along the transaction-time axis.
Aslan-core's `ts.observation.as_of` column is therefore *transaction time*
in the consensus vocabulary, even though it is named after the query
predicate. This is consistent with how Bloomberg point-in-time research
APIs name the parameter.

Conflict alert: Martin Fowler's *Bitemporal History*
(<https://martinfowler.com/articles/bitemporal-history.html>) deliberately
*rejects* the "valid / transaction" naming and uses "actual / record"
instead. Useful as a teaching aid; we keep the consensus glossary
terminology because every standard, paper, and product does.

### 1.2 Sequenced, non-sequenced, current queries

From Snodgrass and the Springer *Encyclopedia of Database Systems* entries
(<https://link.springer.com/rwe/10.1007/978-0-387-39940-9_1053>,
<https://link.springer.com/rwe/10.1007/978-0-387-39940-9_1052>):

- **Current** query: ignores history, returns "the world as of now". Add
  a currency predicate for each correlation name in the FROM clause.
- **Sequenced** query: "give me the history of X" — evaluated independently
  at every instant, with periods preserved in the result. Sequenced joins,
  unions, and aggregates are the hard case; they require period-coalescing
  semantics.
- **Non-sequenced** query: timestamps are just regular columns; SQL
  semantics applies as-is. The vocabulary distinguishes the two
  for the optimiser, not for syntax.

For an append-only research API, the workhorses are:

1. **Current** (entity table only) — `WHERE valid_to = '9999-12-31'`.
2. **Bitemporally-sliced sequenced** at a fixed `as_of` — "what did the
   database know about entity X across its valid-time history, as of T?"
   This is the Bloomberg-style point-in-time replay.
3. **Non-sequenced** — pure forensic queries against the audit log.

### 1.3 The bitemporal rectangle

The 2-D rectangle (valid time on the x-axis, transaction / record time on
the y-axis) is Fowler's framing and originates in Snodgrass. Each fact
occupies a rectangle: (valid-from, valid-to) × (recorded-at, superseded-at).
A point-in-time query at (`vt`, `tt`) returns every rectangle whose interior
contains that point. XTDB's *Building a Bitemporal Index* series
(<https://xtdb.com/blog/building-a-bitemp-index-2-resolution>) is the
clearest published account of the rectangle model. We adopt it as our
mental model; the storage representation is two independent ranges (valid
range + transaction range), not a 2-D type.

---

## 2. Postgres-native primitives

### 2.1 `tstzrange`, GiST `EXCLUDE`, `&&` / `@>`

The Postgres range-types documentation
(<https://www.postgresql.org/docs/current/rangetypes.html>) is the
canonical reference. Salient points for a bitemporal layer:

- `tstzrange` is the timezone-aware range type. Bound inclusivity defaults
  to `[)` — lower-inclusive, upper-exclusive — which matches the SQL:2011
  PERIOD semantics and the half-open convention universally used in
  point-in-time research.
- `EXCLUDE USING gist (entity WITH =, valid WITH &&)` is the canonical
  no-overlap-per-entity constraint; requires the `btree_gist` extension to
  combine equality on a scalar with overlap on a range. This is exactly the
  shape of `ref.identifier`'s existing exclusion constraint
  (`namespace WITH =, value WITH =, daterange(valid_from, valid_to, '[)') WITH &&`)
  and confirms that aslan-core has already chosen the standard pattern.
- The `&&` overlap operator and `@>` containment operator are GiST-indexable
  for ranges; these are the workhorses of point-in-time WHERE clauses
  (e.g. `valid_range @> :as_of_ts`).
- **Multirange** types (Postgres 14+,
  <https://www.cybertec-postgresql.com/en/multiranges-in-postgresql-14/>)
  and `range_agg()` matter when a single entity can have *gaps* in its
  validity — useful for entities that are delisted then relisted. Aslan-core
  models this today with multiple rows; multirange is an optimisation, not
  a contract change.

### 2.2 SQL:2011 `WITHOUT OVERLAPS`, `PERIOD FOR`, `FOR SYSTEM_TIME AS OF`

Kulkarni & Michels, *Temporal features in SQL:2011* (SIGMOD Record vol. 41,
2012, <https://sigmodrecord.org/2012/09/30/temporal-features-in-sql2011/>;
PDF <https://sigmodrecord.org/?smd_process_download=1&download_id=3338>)
describes three categories:

1. **Application-time period tables** — `PERIOD FOR business_time(...)`,
   `WITHOUT OVERLAPS`, `FOR PORTION OF`. This is valid-time.
2. **System-versioned tables** — `PERIOD FOR SYSTEM_TIME(...)` plus
   `WITH SYSTEM VERSIONING`. This is transaction-time and is the
   write-side mechanism (UPDATE / DELETE auto-archive).
3. **Bitemporal tables** — both periods declared on the same table.

Postgres 16 (our target) does **not** implement `PERIOD FOR` or
`FOR SYSTEM_TIME AS OF` natively. Postgres 18 partially implemented
`WITHOUT OVERLAPS` for primary keys, unique constraints, and a `PERIOD`
clause for foreign keys
(<https://www.postgresql.org/docs/current/sql-createtable.html>;
<https://neon.com/postgresql/postgresql-18/temporal-constraints>;
<https://hashrocket.com/blog/posts/postgresql-18-temporal-constraints>);
the wider system-versioning machinery remains absent. The PostgreSQL wiki
SQL2011Temporal page (<https://wiki.postgresql.org/wiki/SQL2011Temporal>)
explicitly states the implementation "is not fully compliant with SQL:2011"
and that in Postgres "the start and end columns can be combined in a
single range type column, or even a multirange." That confirms the
direction aslan-core has already taken.

Implication: we keep using `EXCLUDE USING gist` plus explicit range columns
(or paired `valid_from`/`valid_to` `DATE` columns, as `ref.identifier`
does). When we move to Postgres 18, `WITHOUT OVERLAPS` becomes a
backwards-compatible upgrade; the data model does not have to change.

### 2.3 Partitioning, BRIN, B-tree

- **B-tree on `(entity, ts, as_of DESC)`** is the canonical PIT-replay
  index. `ts.observation` already uses
  `(series_id, ts, as_of DESC)` for the `DISTINCT ON` pattern (see
  migration `20260430_0004_0012_ts_observation_hypertable.py`). This is
  the right primary access path.
- **BRIN** indexes (<https://www.crunchydata.com/blog/postgres-indexing-when-brin-beats-b-tree>;
  <https://www.percona.com/blog/brin-index-for-postgresql-dont-forget-the-benefits/>)
  are extremely cheap to maintain on append-only, naturally-ordered
  columns; they shine when physical row order correlates with the indexed
  column. For `as_of` in an append-only research-API audit table, BRIN is
  worth considering as a *secondary* index for forensic-by-time scans.
  Caveat: BRIN sometimes loses to B-tree for highly selective range
  predicates on huge tables; it is not a universal substitute.
- **Partitioning by `as_of`** (or by `ts`, as `ts.observation` already
  does via Timescale hypertable on `ts`) reduces vacuum/index pressure and
  keeps cold history detached. We deliberately do not recommend a
  partitioning scheme here — that is a Phase-2 implementation decision per
  the brief — but flag that the column to partition on for the research
  API is *transaction time / `as_of`*, not valid time, because retention
  / cold-storage policies operate on when we *learned* something.
- `STABLE` SQL functions, `LATERAL` joins, and materialized views are all
  vanilla Postgres patterns; the Postgres docs are sufficient reference.

---

## 3. Append-only enforcement options

The mission requires that historical state is never silently mutated. The
options, in order of recommended preference for a Postgres-only stack:

### 3.1 Schema-level triggers (BEFORE UPDATE / BEFORE DELETE)

The Postgres trigger documentation
(<https://www.postgresql.org/docs/current/plpgsql-trigger.html>;
<https://www.postgresql.org/docs/current/trigger-definition.html>) shows
two patterns:

- A `BEFORE UPDATE OR DELETE` row-level trigger that `RAISE EXCEPTION`s.
  This is the cleanest for an append-only fact table — the operation
  never reaches the heap.
- A `BEFORE UPDATE` row-level trigger that compares `OLD.col <> NEW.col`
  for *immutable* columns and raises only on those — useful when some
  columns (e.g. `quality_flag`, `metadata`) are explicitly mutable but
  identity columns are not.

Practitioner references:
<https://dev.to/bhanufyi/handling-new-and-old-immutability-in-postgresql-triggers-22jj>;
<https://gist.github.com/diraneyya/ada8660d2e51a1d0949d471e72c81aba>.

Recommended for aslan-core: a single helper trigger function (e.g.
`audit.deny_mutation()`) that any append-only table can attach.

### 3.2 Column-level immutability via DDL

There is no native `IMMUTABLE` column DDL in Postgres. The closest
alternatives are:

- A `GENERATED ALWAYS AS (...) STORED` expression — only useful when the
  value is derived.
- Per-column `BEFORE UPDATE OF col` triggers — narrower than (3.1).

These are weaker than full append-only enforcement and should be combined
with, not substituted for, the trigger pattern.

### 3.3 Postgres rules — discouraged

`CREATE RULE` can rewrite UPDATE / DELETE into no-ops, but the Postgres
maintainers and community consistently steer away from rules in favour of
triggers (rules interact badly with row-level visibility, RETURNING
clauses, and the planner). We reject this option.

### 3.4 Row-level security as a defense-in-depth layer

RLS (<https://www.postgresql.org/docs/current/ddl-rowsecurity.html>) is
**not** a sufficient append-only mechanism on its own — a superuser or
table owner still bypasses it. But, combined with (3.1), a policy of the
form `CREATE POLICY ... FOR UPDATE USING (false)` and a similar `FOR
DELETE` policy is a useful belt-and-braces for application-role connections
(e.g. `aslan_app`). Supabase's *Postgres Auditing in 150 Lines of SQL*
(<https://supabase.com/blog/postgres-audit>) and the LogVault note on the
"audit table anti-pattern" (<https://www.logvault.app/blog/audit-logs-table-anti-pattern>)
both make the same point: RLS is hardening, not enforcement. Audit-log
tables in aslan-core (`audit.events`) already follow this pattern; the
research-API fact tables should too.

### 3.5 GRANT-only INSERT

A simpler hardening: revoke UPDATE / DELETE on the table from every role
including the application role, granting only INSERT (and SELECT). This is
orthogonal to and stacks with (3.1) and (3.4). It is the recommended
*default* for any append-only table that does not need
quality-flag-style mutability.

---

## 4. Point-in-time query patterns

For "what did we know about entity X at valid time `vt` as of transaction
time `tt`?" three Postgres idioms dominate:

### 4.1 `SELECT DISTINCT ON (entity, ts) ... ORDER BY entity, ts, as_of DESC`

Aslan-core's existing canonical PIT pattern (cited in
`20260430_0004_0012_ts_observation_hypertable.py`'s docstring). Strengths:
single-pass, well-understood, Postgres-specific but widely used; supported
by an index on `(series_id, ts, as_of DESC)`. Limitations: historically
slow for "last per group" patterns at scale, though Postgres 18's *Skip
Scan* (<https://www.tigerdata.com/blog/how-we-made-distinct-queries-up-to-8000x-faster-on-postgresql>)
and TimescaleDB's *SkipScan*
(<https://www.tigerdata.com/blog/skip-scan-under-load>) close most of the
gap. Recommended as the default pattern.

### 4.2 `LATERAL` with `ORDER BY ... LIMIT 1`

```
FROM entities e
JOIN LATERAL (
  SELECT ... FROM ts.observation
  WHERE series_id = e.series_id AND ts <= :vt AND as_of <= :tt
  ORDER BY ts DESC, as_of DESC LIMIT 1
) o ON TRUE
```

Strength: one row per outer row; predictable plan; handles cases where
`DISTINCT ON` does not (e.g. enriching with a related row from another
table). Use case: per-entity snapshot queries where the outer set is
small. Limitation: produces N index probes; not appropriate for
million-entity sweeps.

### 4.3 Window functions

`ROW_NUMBER() OVER (PARTITION BY entity ORDER BY ts, as_of DESC)` followed
by a `WHERE rn = 1` filter. Equivalent to (4.1) in semantics, slower in
Postgres in practice because it materialises the window before filtering.
Useful for "top-K per group" (K > 1) where `DISTINCT ON` cannot express
the intent.

### 4.4 Performance characteristics summary

The Postgres planner docs and pgperf-style write-ups
(<https://copyprogramming.com/howto/how-to-make-distinct-on-faster-in-postgresql>)
agree on the ordering: index-backed `DISTINCT ON` ≈ `LATERAL+LIMIT 1` ≪
window-function approach for "latest per group". For the research API,
default to (4.1), reach for (4.2) when the outer cardinality is small,
avoid (4.3) unless K > 1.

The bitemporal-modelling SaaS site `bitemporal.net`
(<https://bitemporal.net/>) and the *PgDay Chicago 2024 Bitemporal*
slides (<https://postgresql.us/events/pgdaychicago2024/sessions/session/1553/slides/130/bitemporal_standard.pdf>)
both confirm the same set of Postgres idioms; nothing exotic is required.

---

## 5. Reference architectures (concept-only — see §7 for ADR-002 rejections)

### 5.1 XTDB / Datomic — concept lift, not runtime

XTDB's docs (<https://v1-docs.xtdb.com/concepts/bitemporality/>;
<https://docs.xtdb.com/about/time-in-xtdb.html>) and JUXT's *The Value of
Bitemporality* (<https://www.juxt.pro/blog/value-of-bitemporality/>) are
the clearest treatment of the bitemporal rectangle in production. XTDB's
*Building a Bitemporal Index*
(<https://xtdb.com/blog/building-a-bitemp-index-2-resolution>) describes a
specialised index structure that we cannot use, but the *resolution*
algorithm — given (vt, tt), pick the row whose valid range covers vt and
whose tx range covers tt, breaking ties by latest tx-start — is the same
algorithm `DISTINCT ON ... ORDER BY ts, as_of DESC` implements over our
schema.

Datomic (<https://docs.datomic.com/peer-tutorial/see-historic-data.html>;
*Datomic Information Model*, <https://www.infoq.com/articles/Datomic-Information-Model/>)
contributes three vocabulary terms worth borrowing without borrowing the
runtime:

- **Basis-T** — a logical, monotonic transaction id. Aslan-core's
  `ingestion_run_id` plays this role; we should use it (in addition to
  `as_of`) as the canonical "what version of the world did we read"
  identifier in the API.
- **`as-of` vs `since`** — point-in-time vs delta semantics. Both are
  expressible as Postgres predicates (`as_of <= :tt` vs `as_of > :tt`)
  but having them as named API verbs is good ergonomics.
- **Append-only / never-mutate** — Datomic's strongest contribution.
  Aligns with our quality bar; reinforces (3.1)+(3.5).

Val Vasilyev's *Datomic: this is not the history you're looking for*
(<https://vvvvalvalval.github.io/posts/2017-07-08-Datomic-this-is-not-the-history-youre-looking-for.html>)
is a useful caution: "history" in append-only stores can be misleading
when corrections happen, which is exactly why bitemporal (not just
append-only) is required.

### 5.2 SAP HANA, SQL Server, MariaDB — concept-only

- **MariaDB** *System-Versioned Tables*
  (<https://mariadb.com/docs/server/reference/sql-structure/temporal-tables/system-versioned-tables>;
  <https://mariadb.com/resources/blog/automatic-data-versioning-in-mariadb-server-10-3/>):
  the syntax `WITH SYSTEM VERSIONING` is the simplest standards-aligned
  expression of transaction time and is a clean reference for *how the
  query surface should read* (`FOR SYSTEM_TIME AS OF :ts`). We cannot
  adopt the storage mechanism (separate history table) cleanly in
  Postgres without `pg_audit`-style triggers, but we can emulate the
  *predicate* exactly as a thin SQL function over `as_of`.
- **SQL Server** Temporal Tables
  (<https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-tables>;
  <https://learn.microsoft.com/en-us/sql/relational-databases/tables/querying-data-in-a-system-versioned-temporal-table>):
  same surface (`FOR SYSTEM_TIME AS OF`, `FROM ... TO ...`,
  `BETWEEN ... AND ...`, `CONTAINED IN`). Useful as a list of
  predicate verbs the research API should expose.
- **SAP HANA** Temporal Tables
  (<https://help.sap.com/docs/SAP_HANA_PLATFORM/6b94445c94ae495c83a19646e7c3fd56/cf3523ab01834f5e84a32164c1fd597a.html>;
  <https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-administration-guide/system-versioned-tables>):
  supports both system-versioning and application-time-period tables, with
  the bitemporal combination explicit. Confirms the convergence of vendor
  approaches around the SQL:2011 surface even where storage differs.

### 5.3 AWS QLDB — out of scope, conceptually relevant

QLDB (<https://aws.amazon.com/qldb/>; AWS migration blog
<https://aws.amazon.com/blogs/database/replace-amazon-qldb-with-amazon-aurora-postgresql-for-audit-use-cases/>)
provides cryptographically-verifiable append-only journals with Merkle
audit proofs. Out of scope for the research API; flagged because if
aslan-core ever needs *cryptographic* (not just operational) audit
evidence, the QLDB-style hash-chained block model can be implemented on
top of Postgres (see the AWS-recommended QLDB-to-Aurora-Postgres migration
path). Not a near-term concern; documented for completeness.

### 5.4 Slowly Changing Dimensions — Type 6

Kimball's *Type 6: Add Type 1 Attributes to Type 2 Dimension*
(<https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/type-6/>;
<https://www.kimballgroup.com/2013/02/design-tip-152-slowly-changing-dimension-types-0-4-5-6-7/>;
Wikipedia <https://en.wikipedia.org/wiki/Slowly_changing_dimension>) is the
data-warehouse community's hybrid: a Type 2 history (insert-on-change with
`valid_from`/`valid_to`) plus Type 1 / Type 3 current-value columns
embedded in each row. This is *single-temporal* (valid time only — there
is no transaction-time axis), so it does not satisfy the Aslan quality
bar on its own. But the schema shape — surrogate-keyed history rows with
`valid_from`/`valid_to` and per-row "current" pointers — matches what
`ref.entity_sector`, `ref.entity_relationship`, and `ref.identifier`
already do. SCD Type 6 is therefore a *valid-time half* of our model; the
transaction-time half lives in the audit columns and `as_of`. Useful as
prior art when we explain the model to data-warehouse-trained users.

---

## 6. Conflicts and resolutions

| # | Conflict | Source A says | Source B says | We adopt | Why |
|---|----------|---------------|---------------|----------|-----|
| 1 | Naming of the two axes | Snodgrass / Jensen / SQL:2011: "valid time" + "transaction time" | Fowler: "actual time" + "record time" | **Snodgrass / SQL:2011 names** | Every standard, paper, vendor, and downstream consumer uses these. Renaming costs interoperability for zero technical benefit. |
| 2 | Period bounds | SQL:2011 PERIOD: closed-open `[)` | Some informal SCD examples use closed-closed `[]` | **Closed-open `[)`** | Matches Postgres `tstzrange` default, SQL:2011, and aslan-core's existing `daterange(..., '[)')` in `ref.identifier`. |
| 3 | Storage shape: paired columns vs single range | aslan-core today: paired `valid_from`/`valid_to` `DATE` columns (`ref.identifier`) plus a derived `daterange()` in the EXCLUDE | Some Postgres tutorials advocate a single `tstzrange` column | **Keep paired columns where they exist; allow either shape on new tables** | Schema continuity with the existing `ref.*` registry. New tables may use a single `tstzrange` if it materially simplifies queries; both are bitemporally equivalent. |
| 4 | Append-only enforcement | Postgres docs: triggers | Some tutorials: rules | **Triggers (BEFORE UPDATE/DELETE) + GRANT-only INSERT, RLS as defense-in-depth** | Rules are discouraged by the Postgres community and interact badly with planner / RETURNING. Triggers are the canonical mechanism. |
| 5 | Index strategy for `as_of` | TimescaleDB / hypertable: chunk on `ts` | Some warehouse patterns: BRIN on `as_of` | **B-tree on `(entity, ts, as_of DESC)` for the hot read path; BRIN on `as_of` only as a secondary forensic index if measured to win** | Matches the existing `ts.observation` index. Don't speculatively add BRIN. |
| 6 | "History" vs "bitemporal" | Datomic / event-sourcing: append-only log = sufficient for audit | Snodgrass / XTDB / Vasilyev's note: append-only log is *not* enough — corrections need bitemporal | **Bitemporal (valid time + transaction time) is required** | The mission requires correct point-in-time replay through restatements; append-only-only is wrong. |
| 7 | Should we adopt `PERIOD FOR SYSTEM_TIME` syntax now? | SQL:2011 / SQL Server / HANA / MariaDB: yes, it's the standard surface | Postgres 16 reality: not supported | **Emulate the surface in our research-API SQL functions / view layer; do not depend on the DDL** | Forwards-compatible: when Postgres adds it, the predicate names already align. |
| 8 | Materialised views for current-state | Some warehouse patterns: yes, refresh on commit | Bitemporal purists: no, keep one source of truth | **Allow as a derived cache, never as the source of truth** | Consistent with aslan-core's `agg.entity_latest_snapshot` pattern (already a derived snapshot, not the truth). |

---

## 7. Off-constraint sources rejected (concept-only per ADR-002)

The following sources propose non-Postgres runtimes for the runtime layer.
**All are rejected as runtimes.** Each is retained as a *concept source*
where listed in §5; the concept lift is explicit and bounded.

| Source | URL | Why concept-only |
|--------|-----|------------------|
| XTDB / Crux | <https://docs.xtdb.com/about/time-in-xtdb.html>; <https://v1-docs.xtdb.com/concepts/bitemporality/>; <https://xtdb.com/blog/building-a-bitemp-index-2-resolution> | Separate temporal DB runtime. ADR-002 rejects. Concept lift: bitemporal rectangle vocabulary, as-of/as-at distinction. |
| Datomic | <https://docs.datomic.com/datomic-overview.html>; <https://docs.datomic.com/peer-tutorial/see-historic-data.html>; <https://www.infoq.com/articles/Datomic-Information-Model/> | Separate temporal DB runtime + JVM dependency. ADR-002 rejects. Concept lift: basis-T, as-of/since/history verbs, append-only invariant. |
| MariaDB | <https://mariadb.com/docs/server/reference/sql-structure/temporal-tables/system-versioned-tables> | Different RDBMS. Concept lift: query-surface naming (`FOR SYSTEM_TIME AS OF`). |
| SQL Server | <https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-tables> | Different RDBMS, commercial. Concept lift: predicate verbs (`AS OF`, `FROM…TO`, `BETWEEN`, `CONTAINED IN`). |
| SAP HANA | <https://help.sap.com/docs/SAP_HANA_PLATFORM/6b94445c94ae495c83a19646e7c3fd56/cf3523ab01834f5e84a32164c1fd597a.html> | Different RDBMS, commercial. Concept lift: explicit bitemporal table syntax (both `PERIOD FOR APPLICATION_TIME` and `PERIOD FOR SYSTEM_TIME`). |
| AWS QLDB | <https://aws.amazon.com/qldb/>; <https://aws.amazon.com/blogs/database/replace-amazon-qldb-with-amazon-aurora-postgresql-for-audit-use-cases/> | Separate ledger DB, AWS-only, retired. Concept lift: cryptographic verification idea (out of near-term scope). |

---

## 8. Open questions for sidar

1. **`ref` registry uses `DATE` for valid-time; `ts.observation` uses
   `TIMESTAMPTZ` for `ts`/`as_of`.** Is the research API allowed to expose
   *both* date-precision and timestamp-precision queries, or should we
   normalise to `TIMESTAMPTZ` at the API boundary even when the underlying
   column is `DATE`? Affects how `as_of` predicates compose across
   `ref.*` and `ts.*` joins.
2. **Basis-T vs `as_of`.** Datomic uses a single monotonic basis-T;
   aslan-core has both `as_of TIMESTAMPTZ` and `ingestion_run_id BIGINT`.
   Should the research API expose `as_of` (timestamp), `ingestion_run_id`
   (logical), or both as PIT predicates? Logical is monotonic and immune
   to clock skew; timestamp is what users want to type.
3. **Append-only enforcement scope.** Should `BEFORE UPDATE/DELETE` deny
   triggers be applied universally to every `ref.*`/`ts.*`/`agg.*` fact
   table, or only to `ts.observation` and the audit tables? Some tables
   (`agg.entity_latest_snapshot`) are explicitly derived caches and need
   to be refreshable.
4. **`FOR SYSTEM_TIME AS OF` surface.** Do we want to expose a thin SQL
   function / view layer that mimics SQL:2011 syntax (e.g.
   `SELECT * FROM observation_as_of('2024-01-01')`), or stick to explicit
   `WHERE as_of <= :tt` predicates in the API? The first is more
   Bloomberg-like; the second is more Postgres-idiomatic.
5. **Multirange vs paired `valid_from`/`valid_to`.** Existing `ref.*`
   tables use paired columns; new tables (e.g. anything modelling
   delisting + relisting cycles) may benefit from `tstzmultirange`. Is
   schema heterogeneity acceptable, or do we standardise on one shape?

---

## 9. Sources

- [A Consensus Glossary of Temporal Database Concepts (Jensen, Dyreson et al., SIGMOD Record 1994/1998)](https://www2.cs.arizona.edu/~rts/pubs/SIGMODRecordMarch94p52.pdf)
- [Consensus glossary mirror (USC InfoLab)](https://infolab.usc.edu/csci599/Fall2001/paper/glossary.pdf)
- [Snodgrass — Developing Time-Oriented Database Applications in SQL (publications page, free PDF)](http://www2.cs.arizona.edu/~rts/publications.html)
- [Snodgrass book (Morgan Kaufmann)](https://shop.elsevier.com/books/developing-time-oriented-database-applications-in-sql/snodgrass/978-0-08-050422-3)
- [Wikipedia — Temporal database](https://en.wikipedia.org/wiki/Temporal_database)
- [Springer — Sequenced Semantics](https://link.springer.com/rwe/10.1007/978-0-387-39940-9_1053)
- [Springer — Nonsequenced Semantics](https://link.springer.com/rwe/10.1007/978-0-387-39940-9_1052)
- [Kulkarni & Michels — Temporal features in SQL:2011 (SIGMOD Record)](https://sigmodrecord.org/2012/09/30/temporal-features-in-sql2011/)
- [Kulkarni & Michels — PDF](https://sigmodrecord.org/?smd_process_download=1&download_id=3338)
- [Wikipedia — SQL:2011](https://en.wikipedia.org/wiki/SQL:2011)
- [Martin Fowler — Bitemporal History](https://martinfowler.com/articles/bitemporal-history.html)
- [PostgreSQL docs — Range Types](https://www.postgresql.org/docs/current/rangetypes.html)
- [PostgreSQL docs — Trigger Functions](https://www.postgresql.org/docs/current/plpgsql-trigger.html)
- [PostgreSQL docs — Trigger Behaviour](https://www.postgresql.org/docs/current/trigger-definition.html)
- [PostgreSQL docs — Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [PostgreSQL docs — CREATE TABLE (Postgres 18, WITHOUT OVERLAPS)](https://www.postgresql.org/docs/current/sql-createtable.html)
- [PostgreSQL wiki — SQL2011Temporal](https://wiki.postgresql.org/wiki/SQL2011Temporal)
- [PostgreSQL wiki — ApplicationTimeProgress](https://wiki.postgresql.org/wiki/ApplicationTimeProgress)
- [Cybertec — Multiranges in PostgreSQL 14](https://www.cybertec-postgresql.com/en/multiranges-in-postgresql-14/)
- [Cybertec — Exclusion constraints in PostgreSQL](https://www.cybertec-postgresql.com/en/exclusion-constraints-in-postgresql-and-a-tricky-problem/)
- [Crunchy Data — Better Range Types in Postgres 14](https://www.crunchydata.com/blog/better-range-types-in-postgres-14-turning-100-lines-of-sql-into-3)
- [Crunchy Data — Postgres Indexing: When Does BRIN Win?](https://www.crunchydata.com/blog/postgres-indexing-when-does-brin-win)
- [Percona — BRIN Index for PostgreSQL](https://www.percona.com/blog/brin-index-for-postgresql-dont-forget-the-benefits/)
- [Tiger Data — DISTINCT ON 8000x faster](https://www.tigerdata.com/blog/how-we-made-distinct-queries-up-to-8000x-faster-on-postgresql)
- [Tiger Data — TimescaleDB SkipScan under load](https://www.tigerdata.com/blog/skip-scan-under-load)
- [Neon — PostgreSQL 18 Temporal Constraints](https://neon.com/postgresql/postgresql-18/temporal-constraints)
- [Hashrocket — PostgreSQL 18 Temporal Constraints](https://hashrocket.com/blog/posts/postgresql-18-temporal-constraints)
- [Aiven — Postgres 18 conquers time with temporal constraints](https://aiven.io/blog/exploring-how-postgresql-18-conquered-time-with-temporal-constraints)
- [depesz — Waiting for PostgreSQL 18 (temporal PK)](https://www.depesz.com/2024/09/30/waiting-for-postgresql-18-add-temporal-primary-key-and-unique-constraints/)
- [depesz — Waiting for PostgreSQL 18 (temporal FK)](https://www.depesz.com/2024/10/03/waiting-for-postgresql-18-add-temporal-foreign-key-contraints/)
- [Better Stack — Temporal Constraints in PostgreSQL 18](https://betterstack.com/community/guides/databases/postgres-temporal-constraints/)
- [Bitemporal.net — Temporal algorithms / SQL for warehousing](https://bitemporal.net/)
- [PgDay Chicago 2024 — Bitemporal SQL slides](https://postgresql.us/events/pgdaychicago2024/sessions/session/1553/slides/130/bitemporal_standard.pdf)
- [Red Gate Simple Talk — PostgreSQL Temporal Tables](https://www.red-gate.com/simple-talk/databases/postgresql/saving-data-historically-with-temporal-tables-part-1-queries/)
- [pg_bitemporal extension reference](https://github.com/scalegenius/pg_bitemporal/blob/master/docs/pg_bitemporal_reference.md)
- [Vlad Mihalcea — How do PostgreSQL advisory locks work](https://vladmihalcea.com/how-do-postgresql-advisory-locks-work/)
- [pgPedia — pg_advisory_xact_lock](https://pgpedia.info/p/pg_advisory_xact_lock.html)
- [Supabase — Postgres Auditing in 150 lines of SQL](https://supabase.com/blog/postgres-audit)
- [LogVault — audit_logs table anti-pattern](https://www.logvault.app/blog/audit-logs-table-anti-pattern)
- [Sequel Postgres triggers — immutable columns / counter caches](https://github.com/jeremyevans/sequel_postgresql_triggers)
- [DEV.to — Handling NEW/OLD immutability in Postgres triggers](https://dev.to/bhanufyi/handling-new-and-old-immutability-in-postgresql-triggers-22jj)
- [GitHub gist — triggers to prevent insert/update/delete](https://gist.github.com/diraneyya/ada8660d2e51a1d0949d471e72c81aba)
- [XTDB — Time in XTDB](https://docs.xtdb.com/about/time-in-xtdb.html)
- [XTDB v1 docs — Bitemporality](https://v1-docs.xtdb.com/concepts/bitemporality/)
- [XTDB blog — Building a Bitemporal Index (part 2)](https://xtdb.com/blog/building-a-bitemp-index-2-resolution)
- [JUXT — The Value of Bitemporality](https://www.juxt.pro/blog/value-of-bitemporality/)
- [Datomic — See Historic Data](https://docs.datomic.com/peer-tutorial/see-historic-data.html)
- [Datomic — Overview](https://docs.datomic.com/datomic-overview.html)
- [InfoQ — The Datomic Information Model](https://www.infoq.com/articles/Datomic-Information-Model/)
- [Vasilyev — Datomic: this is not the history you're looking for](https://vvvvalvalval.github.io/posts/2017-07-08-Datomic-this-is-not-the-history-youre-looking-for.html)
- [MariaDB — System-Versioned Tables](https://mariadb.com/docs/server/reference/sql-structure/temporal-tables/system-versioned-tables)
- [MariaDB blog — Automatic Data Versioning in 10.3](https://mariadb.com/resources/blog/automatic-data-versioning-in-mariadb-server-10-3/)
- [MariaDB blog — Rewinding Time (system + application versioning)](https://mariadb.com/resources/blog/rewinding-time-in-mariadb-databases-system-versioning-and-application-time/)
- [Microsoft Learn — Temporal Tables (SQL Server)](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-tables?view=sql-server-ver17)
- [Microsoft Learn — Querying a system-versioned temporal table](https://learn.microsoft.com/en-us/sql/relational-databases/tables/querying-data-in-a-system-versioned-temporal-table?view=sql-server-ver17)
- [SAP — Temporal Tables (HANA Platform)](https://help.sap.com/docs/SAP_HANA_PLATFORM/6b94445c94ae495c83a19646e7c3fd56/cf3523ab01834f5e84a32164c1fd597a.html)
- [SAP — System-Versioned Tables (HANA Cloud)](https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-administration-guide/system-versioned-tables)
- [SAP — Application Time-Period Tables (HDI)](https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-deployment-infrastructure-hdi-reference/application-time-period-tables-hdbapplicationtime)
- [AWS — Amazon QLDB](https://aws.amazon.com/qldb/)
- [AWS Database blog — Replace Amazon QLDB with Amazon Aurora PostgreSQL for audit](https://aws.amazon.com/blogs/database/replace-amazon-qldb-with-amazon-aurora-postgresql-for-audit-use-cases/)
- [Kimball Group — Type 6: Add Type 1 Attributes to Type 2 Dimension](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/type-6/)
- [Kimball Group — Design Tip 152: SCD Types 0, 4, 5, 6, 7](https://www.kimballgroup.com/2013/02/design-tip-152-slowly-changing-dimension-types-0-4-5-6-7/)
- [Wikipedia — Slowly Changing Dimension](https://en.wikipedia.org/wiki/Slowly_changing_dimension)
