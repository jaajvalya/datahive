# Implementation Roadmap

Build order matters. The temptation is to start with connectors because they are the visible
part. That is backwards — connectors written before the substrate exists get rewritten.

---

## Phase 0 — Substrate (weeks 1–4)

Nothing source-specific. Get these right and every connector afterwards is small.

- [ ] Core models: `SourceRef`, `ExtractionTask`, `Batch`, `Position`, `Envelope`
- [ ] Sink protocol + `ObjectStoreSink` (Parquet, deterministic paths)
- [ ] State store: watermarks, positions, leases (Postgres, with row-level locking)
- [ ] Audit ledger: hash chain, WORM writer, chain-head publisher
- [ ] Budget governor: token buckets + cumulative caps
- [ ] Secret broker: pluggable vault backends, short-TTL minting
- [ ] FastAPI control plane: registry, task lease/commit, OpenAPI
- [ ] Agent loop: lease → execute → heartbeat → commit, with WAL
- [ ] Redaction-enforcing log formatter (before any connector can log anything)

**Exit criteria:** a fake "generator" source can run end to end, survive a `kill -9` mid-batch,
and produce a clean audit chain with no duplicates in the sink.

Test that last property deliberately. Idempotent replay is much harder to retrofit than to
build in, and a fake source lets you kill the agent a hundred times in a loop.

## Phase 1 — First real connector: PostgreSQL (weeks 5–7)

Postgres first, because it exercises every strategy on a source you can run locally: full
snapshot, composite watermark, and logical-replication CDC with snapshot handover.

- [ ] Partitioned full snapshot with a stable point-in-time
- [ ] Composite `(ts, pk)` watermark with bounded upper edge
- [ ] Logical replication slot, `pgoutput` decoding, position persistence
- [ ] Snapshot → stream handover
- [ ] Slot-lag metric + alert (do not ship CDC without this)
- [ ] Key-set reconciliation job

**Exit criteria:** run against a database under concurrent write load and reconcile to zero
divergence across a full → incremental → CDC transition.

## Phase 2 — Analytics sources (weeks 8–11)

- [ ] `ObjectStoreReader`: Parquet/ORC/CSV/JSON with projection and predicate pushdown
- [ ] Lakehouse: Iceberg manifest + Delta log readers, snapshot diff, **merge-on-read deletes**
- [ ] Warehouse: Snowflake `COPY INTO` + stage read; BigQuery `EXPORT` / Storage Read API
- [ ] Lake: partition pruning, event-notification consumer, inventory-report diff
- [ ] `dry_run` cost estimation wired into the budget governor

**Exit criteria:** a 100 GB extract that consumes under one minute of provider compute.

## Phase 3 — Collaboration and mail (weeks 12–16)

- [ ] Graph auth: certificate-based client credentials, `Sites.Selected`
- [ ] Graph delta connector, shared by SharePoint and OneDrive
- [ ] Google Drive `changes.list` connector with native-format export
- [ ] Mailbox: Graph mail delta; IMAP `UIDVALIDITY`/`MODSEQ` fallback
- [ ] ACL capture and propagation
- [ ] Content extraction, MIME walk, attachment dedupe by hash
- [ ] Throttle handler honouring `Retry-After` exactly, with AIMD concurrency

**Exit criteria:** a 100k-document tenant sync with zero `429`s sustained over an hour, and
ACLs verifiably matching the source.

## Phase 4 — Hardening (weeks 17–20)

- [ ] Remaining sinks: `RdbmsSink`, `StreamSink`
- [ ] Envelope encryption with tenant KMS + crypto-shred path
- [ ] Schema drift postures (strict / evolve / permissive) end to end
- [ ] Reconciliation framework across all source families
- [ ] Consumption reporting to providers
- [ ] Access-drift verification job
- [ ] Chaos testing: kill agents, expire tokens, revoke credentials mid-run, invalidate CDC
      positions, corrupt a WAL segment
- [ ] Penetration test focused on log/error leakage and identifier injection

## Phase 5 — Scale-out (ongoing)

Oracle, SQL Server, Db2, Teradata, MongoDB, Redshift, Synapse, Hudi. Each is now a plugin
against a proven substrate rather than a project.

---

## Team shape

| Role | Focus |
|---|---|
| Data architect | Strategy selection per source, provider negotiation, cost contracts |
| Backend engineers ×2–3 | Substrate, control plane, connectors |
| Security engineer (part-time) | Threat model, key management, audit chain, pen test |
| SRE (part-time from Phase 1) | Slot-lag alerting, budget monitoring, incident runbooks |

## What to negotiate with each provider, at onboarding

Get these in writing before you write a line of connector code. Every one of them is
cheap for the provider and expensive for you to work around later.

1. **Read replica** access rather than the primary
2. **Dedicated compute** — warehouse, WLM queue, resource group, or app registration
3. **Explicit object allowlist** with read-only grants, and a named contact who owns it
4. **CDC/log access** if available, plus agreement on who monitors lag
5. **Event notifications** on lake buckets
6. **Blackout windows** from their calendar
7. **A byte/credit budget** with an agreed hard stop
8. **A named technical contact** for when something goes wrong at 3 a.m.

Point 8 is the one people forget and the one that matters most during the first incident.

---

## Anti-patterns to reject in review

- `SELECT *` anywhere in a connector
- A watermark without an upper bound
- Watermark advanced before sink commit
- Any query without a statement timeout
- Client-side masking of a column that was fetched from the source
- Retry logic that ignores `Retry-After`
- Full bucket listing where partition pruning is possible
- Row values, query text with bound parameters, or secrets in any log line
- CDC shipped without slot-lag alerting
- A connector that cannot be killed mid-run and resumed
- Reading a lakehouse table's files while ignoring its delete files
