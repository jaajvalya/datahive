# ID360 Connector Framework — Architecture

## 1. The problem stated precisely

You are building a **tenant-side ingestion system** that pulls data out of somebody else's
enterprise data platform. That framing drives every design decision in this document:

- **You do not own the source.** You cannot add indexes, you cannot install agents on their
  database hosts without a change request, you cannot assume a maintenance window.
- **Every byte you read costs the provider money** — warehouse credits, egress charges,
  Graph API throttling budget, IOPS on a production OLTP box.
- **You are a data custodian, not a data owner.** Everything you touch has to be encrypted,
  attributable to a named principal, and reconstructable in an audit.

So the framework is not "eight connectors". It is **one execution substrate** with a small,
strictly-bounded plugin surface, and eight plugin families that sit on top of it.

---

## 2. Topology: hybrid agent + cloud control plane

```
Provider network (on-prem or their cloud VPC)      Your network (ID360 cloud)
┌──────────────────────────────────────┐           ┌────────────────────────────┐
│  Data sources                        │           │  Control plane (FastAPI)   │
│  Oracle / Db2 / SQL Server           │           │  - connection registry     │
│  Teradata / Netezza                  │           │  - schedule + job graph    │
│  Databricks / Snowflake / BigQuery   │           │  - policy engine           │
│  HDFS / S3 / ADLS                    │           │  - audit ledger            │
│  SharePoint / OneDrive / M365        │           │  - schema registry         │
│         ▲                            │           │  - secret broker           │
│         │ (private, never leaves)    │           └────────────┬───────────────┘
│  ┌──────┴──────────────────────┐     │                        │ mTLS,
│  │  ID360 Agent (stateless)    │─────┼────────────────────────┘ outbound only
│  │  - task executor            │     │   long-poll / gRPC stream
│  │  - extraction strategies    │     │
│  │  - budget governor          │     │
│  │  - local encryption + WAL   │     │
│  └──────┬──────────────────────┘     │
│         │ writes bulk data directly  │
│         ▼                            │
│  ┌─────────────────────────────┐     │
│  │  Sink (object store / RDBMS)│     │  ← may be on-prem OR cloud
│  └─────────────────────────────┘     │
└──────────────────────────────────────┘
```

### Why this shape

**Outbound-only from the provider.** The agent dials the control plane; the control plane
never dials in. No inbound firewall rule, no VPN, no static NAT. This single property is what
gets the design past most enterprise security reviews.

**Control plane never touches bulk data.** It carries task descriptors, watermarks, schema
fingerprints, metrics, and audit events — all small, all structured. Bulk rows and file bytes
go agent → sink over a path that never traverses your control plane. Consequences:

- Your control plane is not in the data blast radius. A compromise there leaks metadata,
  not customer records.
- The control plane scales with *number of jobs*, not *volume of data*.
- Data residency is satisfiable: the agent and sink can both stay in-country while the
  control plane sits elsewhere.

**The agent is stateless between runs.** All durable state (watermarks, checkpoints, schema
versions, delivery ledger) lives in the control plane. Agents are cattle: kill one mid-run and
another picks up from the last committed checkpoint. What the agent *does* keep locally is a
short-lived encrypted spool (a write-ahead log) so an in-flight batch is not lost on a restart.

### When to deviate

| Situation | Topology |
|---|---|
| Provider forbids *any* egress from the data zone | Fully air-gapped agent; control plane replica on-prem, syncs by signed bundle export |
| Source is a managed cloud warehouse and you have a private link | Skip the agent; run the connector in your own cloud over PrivateLink/Private Service Connect |
| Source is SaaS with only public endpoints (M365, Google) | Agent optional; run connectors in your cloud, but keep the agent option for tenants who insist traffic originates from their IP allowlist |

---

## 3. Planes and layers

### 3.1 Control plane responsibilities

| Component | Responsibility |
|---|---|
| **Connection registry** | Source definitions, capability flags, secret *references* (never secret values) |
| **Scheduler** | Turns pipelines into a DAG of tasks; enforces concurrency and blackout windows |
| **Policy engine** | Evaluates whether a task is allowed: object allowlist, column masking, row filters, max-bytes-per-window |
| **Schema registry** | Versioned schema fingerprints; drift detection and evolution rules |
| **State store** | Watermarks, LSN/SCN positions, delta tokens, checkpoint offsets, per-partition status |
| **Audit ledger** | Append-only, hash-chained record of every decision and every extraction |
| **Secret broker** | Mints short-lived credentials from the tenant's own vault; never stores long-lived keys |

### 3.2 Agent responsibilities

| Component | Responsibility |
|---|---|
| **Task executor** | Leases a task, runs it, heartbeats, reports terminal state |
| **Strategy resolver** | Picks the extraction technique given source capabilities + policy + history |
| **Budget governor** | Token-bucket over rows, bytes, query-seconds, and API calls; hard stop on breach |
| **Reader plugins** | Source-specific I/O only — no scheduling, no state, no crypto decisions |
| **Encoder** | Normalizes to Arrow → writes Parquet with the framework's canonical envelope |
| **Sink writer** | Pluggable: object store + table format, RDBMS, or stream |
| **Local WAL** | Encrypted spool for in-flight batches; enables exactly-once commit semantics |

---

## 4. The five extraction strategies

Every connector, regardless of source, resolves to one of five strategies. This is the core
abstraction that keeps eight source families from becoming eight separate codebases.

| # | Strategy | Mechanism | Source load | Latency | Correctness |
|---|---|---|---|---|---|
| 1 | **Full snapshot** | Read everything | Highest | Batch | Perfect |
| 2 | **Incremental by watermark** | `WHERE updated_at > :hwm` | Low | Batch/micro-batch | Misses hard deletes; needs monotonic column |
| 3 | **Log-based CDC** | Read the DB's own redo/WAL/binlog | Near-zero on query path | Seconds | Complete incl. deletes |
| 4 | **Change-feed / delta token** | Source hands you a change cursor (Graph delta, Drive changes, Iceberg/Delta snapshots) | Near-zero | Minutes | Complete if token never expires unhandled |
| 5 | **Storage-native / zero-copy** | Read the underlying files directly, or have the source `UNLOAD`/`EXPORT` to object storage | Near-zero on the engine | Batch | Perfect |

**Strategy resolution order** — always prefer the cheapest strategy the source can support:

```
5 (storage-native)  →  3 (log CDC)  →  4 (change feed)  →  2 (watermark)  →  1 (full)
```

Full snapshot is never the steady state. It is a bootstrap operation and a periodic
reconciliation operation, and both are scheduled into explicit low-traffic windows.

See [02-extraction-strategies.md](02-extraction-strategies.md) for the mechanics of each.

---

## 5. Canonical data envelope

Every connector emits the same envelope regardless of source. This is what makes the sink
layer pluggable and the lineage story coherent.

```python
{
  # ---- payload ----
  "record": {...},                  # source-native fields, type-normalized

  # ---- provenance (system columns, prefixed _id360) ----
  "_id360_source_id":   "uuid",     # which registered connection
  "_id360_object":      "SALES.ORDERS",
  "_id360_op":          "I|U|D|R",  # insert/update/delete/refresh-snapshot
  "_id360_extract_ts":  "2026-07-20T09:14:22.113Z",
  "_id360_source_ts":   "2026-07-20T09:14:21.980Z",  # commit time at source, if known
  "_id360_position":    "scn:12345678",              # LSN/SCN/token/offset
  "_id360_batch_id":    "uuid",
  "_id360_run_id":      "uuid",
  "_id360_schema_ver":  7,
  "_id360_row_hash":    "sha256:...",                # for reconciliation
  "_id360_pii_class":   "restricted|confidential|internal|public"
}
```

`_id360_position` plus `_id360_row_hash` is what makes replays idempotent and reconciliation
cheap — you can compare hash sets between source and sink without moving data again.

---

## 6. Delivery semantics

The framework targets **at-least-once delivery with idempotent commit**, which yields
effectively-exactly-once at the table level.

```
1. Agent leases task           → control plane records lease, TTL, agent identity
2. Agent reads batch N         → writes to encrypted local WAL
3. Agent writes batch N to sink → object path keyed by (run_id, batch_id), immutable
4. Agent calls commit(N)       → control plane atomically: advance watermark
                                  + append audit record + mark batch durable
5. Crash anywhere before 4     → batch replayed; sink path is deterministic so the
                                  re-written object overwrites byte-identically;
                                  table-format commit is idempotent on batch_id
```

The watermark advances **only after** the sink acknowledges durability. Never the other way
round. This is the single most common correctness bug in hand-rolled connectors.

For table formats (Iceberg/Delta) the commit is a real ACID transaction, so a partial run is
invisible to readers. For RDBMS sinks the framework uses staging table + atomic swap or
`MERGE` keyed on `_id360_batch_id`.

---

## 7. Schema drift

Three configurable postures per source:

| Posture | Behaviour on new column | On type widening | On breaking change |
|---|---|---|---|
| `strict` | Fail the run, alert | Fail | Fail |
| `evolve` (default) | Add to sink schema, backfill null | Widen (int→bigint, decimal precision up) | Quarantine batch, alert, keep pipeline running on last-good schema |
| `permissive` | Add | Widen or cast | Cast with lossy-cast counter in metrics |

Schema is fingerprinted (sorted `name:type:nullable` triples, SHA-256) at the start of every
run and compared to the registry. Cheap, and it catches drift *before* you read a single row.

---

## 8. Failure model

| Failure | Detection | Response |
|---|---|---|
| Source unavailable | Connect timeout | Exponential backoff with jitter, capped; circuit breaker opens after N consecutive |
| Source throttling (429/503) | Status code + `Retry-After` | Honour `Retry-After` exactly; halve concurrency; do not retry-storm |
| Query exceeds budget | Budget governor | Kill query at source (`pg_cancel_backend`, Snowflake `SYSTEM$CANCEL_QUERY`), split partition, retry smaller |
| Agent crash mid-batch | Missing heartbeat, lease expiry | Lease released, another agent replays from checkpoint |
| Sink unavailable | Write failure | Spool to local WAL up to configured cap, then backpressure the reader |
| Schema drift | Fingerprint mismatch | Per posture above |
| Poison record | Encode failure | Route to dead-letter with full context; continue batch; alert on DLQ rate |
| Watermark regression | Ordering check | Refuse to move watermark backwards without an explicit operator override token |

Circuit breaker states are per-`(source, object)`, not per-source, so one hot table cannot
take down the whole connection.

---

## 9. Related documents

- [02-extraction-strategies.md](02-extraction-strategies.md) — the five strategies in depth
- [03-source-families.md](03-source-families.md) — per-source-type approaches and trade-offs
- [04-security.md](04-security.md) — encryption, identity, secrets, network
- [05-audit-and-observability.md](05-audit-and-observability.md) — audit ledger, tracing, metrics
- [06-performance-and-cost-guardrails.md](06-performance-and-cost-guardrails.md) — being a good tenant
- [07-implementation-roadmap.md](07-implementation-roadmap.md) — build order
