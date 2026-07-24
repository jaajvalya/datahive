# Extraction Strategies

Five strategies. Every connector in the framework resolves to one of them. This document
explains the mechanics, the failure modes, and when each is the right choice.

---

## Strategy 1 — Full snapshot

**Mechanism.** Read the entire object. `SELECT * FROM t`, or enumerate every file, or list
every document in a drive.

**When it is correct to use it**

- Bootstrap, once, before switching to an incremental strategy.
- Small dimension/reference tables where incremental logic costs more than it saves
  (rule of thumb: under ~1M rows or ~1 GB, full refresh nightly is simpler and cheaper).
- Periodic reconciliation — quarterly, in a maintenance window, to catch silent drift.
- Sources with no change signal whatsoever (a CSV drop, a view over a legacy system).

**How to make it not hurt**

The naive `SELECT *` streamed through a single cursor is the single worst thing you can do to
a production database. Instead:

1. **Partition the read.** Split on a numeric PK, a date column, or a hash of the PK, and run
   N partitions with bounded parallelism. Each partition is an independently retryable unit.
2. **Read from a replica, not the primary.** Always ask. Most enterprises have one.
3. **Use the engine's bulk export path** rather than the row protocol — see Strategy 5.
4. **Server-side cursors with a fetch size**, never client-side buffering of the full result.
5. **Consistent point-in-time**: use a repeatable-read snapshot / `AS OF` timestamp so
   partitions don't tear against concurrent writes.

```sql
-- Partitioned snapshot: partition p of n, with a stable point-in-time
SELECT * FROM sales.orders
 WHERE MOD(ABS(HASHTEXT(order_id::text)), :n) = :p   -- Postgres
   AND created_at < :snapshot_ts                     -- stable boundary
```

**Cost profile.** O(table size) every run. Unbounded. This is why it is never the steady state.

---

## Strategy 2 — Incremental by watermark

**Mechanism.** Track the maximum value of a monotonically non-decreasing column and read only
rows above it.

```sql
SELECT * FROM sales.orders
 WHERE updated_at > :last_watermark
   AND updated_at <= :run_boundary     -- CRITICAL: bound the top end
 ORDER BY updated_at
```

**The three bugs everyone writes**

1. **No upper bound.** Without `<= :run_boundary` you race against in-flight commits: a row
   committed *during* your read with a timestamp *below* your new watermark is invisible
   forever. Always close the interval at a boundary that is safely in the past.

2. **Boundary set to `now()`.** A transaction that started before your read but commits after
   it will carry a timestamp inside your window that you never saw. Set the boundary to
   `now() - safety_lag`, where `safety_lag` exceeds your longest expected transaction
   (30 s–5 min typically), or use a strictly transactional position (Strategy 3).

3. **Strict `>` on a non-unique column.** If 10,000 rows share the exact same `updated_at`,
   and you crash mid-batch, `>` skips the remainder. Use a **composite watermark**
   `(updated_at, pk)` and a lexicographic predicate:

```sql
WHERE (updated_at, order_id) > (:hwm_ts, :hwm_pk)
  AND updated_at <= :run_boundary
ORDER BY updated_at, order_id
```

**What it cannot do.** Hard deletes are invisible. Mitigations, in order of preference:
switch to CDC; ask the source to soft-delete; or run a periodic **key-set reconciliation** —
pull only the PK column (cheap, index-only scan), diff against your sink's key set, and mark
the missing ones deleted. Weekly key-set diff + daily watermark incremental is a very common
and very defensible compromise.

**Index requirement.** `updated_at` must be indexed or every run is a full table scan that
looks incremental. Verify with `EXPLAIN` during onboarding and record the plan in the
connection registry. If there is no index and the DBA will not add one, you do not have a
watermark strategy — say so early.

**Cost profile.** O(changed rows) if indexed. O(table) if not.

---

## Strategy 3 — Log-based CDC

**Mechanism.** Read the database's own transaction log — the thing it already writes for
crash recovery and replication. Zero additional query load, complete change history including
deletes, and transactionally exact ordering.

| Engine | Log interface | Notes |
|---|---|---|
| PostgreSQL | Logical replication slot, `pgoutput` / `wal2json` | Native, no license. **Slots retain WAL** — an abandoned slot fills the disk and takes down the primary. Monitor lag ruthlessly. |
| MySQL / MariaDB | Row-format binlog, replication protocol | Register as a replica. Needs `binlog_format=ROW`, `binlog_row_image=FULL`. |
| SQL Server | CDC tables (`cdc.fn_cdc_get_all_changes_*`) or Change Tracking | CDC gives before/after images; Change Tracking gives only "this key changed" — much lighter, but requires a follow-up read. |
| Oracle | LogMiner, or XStream/GoldenGate API (licensed) | LogMiner is free but heavy on the source. Consider Change Tracking-style alternatives first. |
| Db2 | SQL replication capture / journals (i-series) | Usually needs DBA setup. |
| MongoDB | Change streams (oplog) | Resume tokens; watch for oplog rollover. |

**The resume-position contract.** CDC is only as good as your position handling:

- Persist the position **after** the sink commit, never before.
- The log is finite. If your connector is down longer than the retention window, the position
  becomes invalid and you **must** detect that explicitly and trigger a re-snapshot, not
  silently resume from the earliest available position (which produces silent data loss at the
  head or duplicate storms).
- Emit heartbeat/keepalive positions on idle sources so slot lag does not grow while nothing
  is changing — this is what prevents the "abandoned slot filled the WAL disk" incident.

**Snapshot-to-stream handover.** The hard part of CDC is the initial load. The correct order:

```
1. Open the log position P0 and start buffering (do not process yet)
2. Take a consistent snapshot AS OF P0 (or with an export-consistent lock/snapshot)
3. Write snapshot rows with _id360_op = 'R'
4. Replay buffered log from P0 forward, applying by primary key
5. Because sink apply is upsert-by-PK and log records are ordered, any overlap
   between snapshot and log converges correctly
```

If you cannot get a log position that is causally consistent with the snapshot, use the
watermark-based incremental-snapshot approach (open a low/high watermark around each snapshot
chunk and drop log events for keys emitted in that chunk between the watermarks). That is the
DBLog algorithm and it lets you snapshot without any locking at all.

**Cost profile.** Near-zero incremental load on the query path. Real cost is operational:
someone has to care about slot lag, log retention, and DBA permissions.

**Permission reality check.** Log-based CDC requires elevated privileges (`REPLICATION`,
`db_owner` to enable SQL Server CDC, LogMiner grants). Many enterprises will refuse. Have
Strategy 2 or 5 ready as the fallback and do not architect yourself into a corner.

---

## Strategy 4 — Change feed / delta token

**Mechanism.** The source maintains the change list for you and hands you an opaque cursor.
You are not scanning anything; you are reading a queue the source already built.

| Source | Feed | Cursor |
|---|---|---|
| Microsoft Graph (SharePoint, OneDrive, Outlook) | `/delta` endpoints | `@odata.deltaLink` |
| Google Drive | `changes.list` | `startPageToken` / `nextPageToken` |
| Delta Lake | Change Data Feed | version range `_change_type` |
| Apache Iceberg | Incremental scan between snapshots | `snapshot-id` range |
| Azure Cosmos / DynamoDB | Change feed / Streams | continuation token / shard iterator |
| Kafka-fronted platforms | Consumer offsets | offset per partition |

**Token handling rules**

1. Tokens are **opaque**. Never parse, never construct, never assume format stability.
2. Tokens **expire**. Graph delta links and Drive page tokens go stale (typically days to
   weeks of inactivity). Handle the specific "resync required" error
   (`410 Gone` / `resyncRequired`) by falling back to a full enumeration — and *alert*, because
   a resync is expensive and you want to know it happened.
3. Persist the token only after sink commit, same rule as CDC.
4. Feeds usually give you **"this changed"**, not the content. Budget for the follow-up reads,
   and dedupe: a document edited 40 times in an hour appears 40 times in the feed but you only
   need to fetch it once per run.

**Cost profile.** Cheapest possible for file/collaboration sources. The dominant cost becomes
the content fetch, not the change detection.

---

## Strategy 5 — Storage-native / zero-copy

**Mechanism.** Do not make the query engine materialize rows for you. Either read the
underlying files directly, or ask the engine to write them out once, in bulk, in a format that
costs it almost nothing.

### 5a. Direct file read (lakehouse and lake)

For Iceberg, Delta, and Hudi, the table metadata *is* an API. Read the manifest, get the
Parquet file list, read those files straight from object storage. **The query engine is never
involved — zero warehouse credits, zero cluster time.**

```
Read table metadata  →  resolve snapshot N  →  get file list + partition stats
   →  prune by partition predicate  →  read only the needed Parquet files from S3/ADLS
```

For incremental: diff the file list between snapshot N and snapshot M. Files added since M are
your new data. This is dramatically cheaper than any `SELECT`.

The only cost is object storage `GET` requests and egress — and you can eliminate egress
entirely by running the agent in the same region/datacentre as the bucket.

### 5b. Engine-native bulk unload (warehouse)

Warehouses charge by compute time. A row-by-row cursor holds a warehouse up for the entire
transfer. A bulk `UNLOAD`/`EXPORT` runs a short, highly-parallel job and then the warehouse
goes idle while you read files at your own pace.

| Engine | Command | Lands in |
|---|---|---|
| Snowflake | `COPY INTO @stage/... FROM (SELECT ...) FILE_FORMAT=(TYPE=PARQUET)` | External stage on S3/Azure/GCS |
| BigQuery | `EXPORT DATA OPTIONS(...) AS SELECT ...`, or the free **Storage Read API** | GCS / direct Arrow stream |
| Redshift | `UNLOAD ('SELECT ...') TO 's3://...' FORMAT PARQUET PARALLEL ON` | S3 |
| Synapse | `CREATE EXTERNAL TABLE AS SELECT` (CETAS) | ADLS |
| Databricks | Write to external location, or read the Delta files directly (5a) | ADLS/S3 |
| Teradata | TPT / FastExport | Local or object store |
| Oracle | Data Pump to a directory object | Filesystem |

The economics are stark. A 500 GB extract through a JDBC cursor might hold an X-Small
warehouse for six hours. The same extract via `COPY INTO` is roughly ten minutes of compute
plus a file read you do on your own time. **Roughly an order of magnitude difference in the
provider's bill, for the same bytes delivered.**

### 5c. Streaming result APIs

Where a bulk unload is unavailable, prefer the engine's columnar result API over the row
protocol: BigQuery Storage Read API, Databricks Cloud Fetch / Arrow, Snowflake's Arrow result
format, ADBC drivers generally. These avoid row-by-row serialization and typically move
5–20× more data per unit of engine time than ODBC/JDBC row fetch.

**Cost profile.** The cheapest strategy in every dimension. Always check whether it is
available before considering anything else.

---

## Decision procedure

Run this at onboarding, per object, and record the answer in the connection registry:

```
Is the object backed by an open table format (Iceberg/Delta/Hudi)
  or raw files I can read directly?
        └─ YES → Strategy 5a. Incremental via snapshot diff. Done.

Does the source expose a change feed with a durable cursor?
        └─ YES → Strategy 4. Bootstrap with 5b/1, then feed. Done.

Can I get log-level CDC, and will the DBA grant it,
  and is someone willing to own slot/log-lag monitoring?
        └─ YES → Strategy 3. Snapshot handover per §3. Done.

Is there an indexed, monotonic, reliable change column?
        └─ YES → Strategy 2 + periodic key-set reconciliation for deletes.
                 Bootstrap with 5b if available.

Otherwise → Strategy 1, partitioned, off a replica, in a maintenance window,
            at the lowest frequency the business will accept.
            Then go back to the provider and negotiate for something better.
```

The last line is not a joke. If you end up at Strategy 1 on a large table, that is a signal to
escalate — the technical fix is cheap for the provider and the ongoing cost of full refreshes
is not.
