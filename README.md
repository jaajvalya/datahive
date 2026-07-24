# ID360 — Bespoke Enterprise Data Connectors

A framework for pulling data out of somebody else's enterprise data platform,
without third-party ingestion tools, without hurting their performance, and
without inflating their bill.

Eight source families sit on **one execution substrate** with a small,
strictly-bounded plugin surface. That is the central design decision: the hard
parts — idempotent delivery, watermarks, budgets, audit, encryption — are solved
once, and a connector is only source-specific I/O.

---

## Start here

| Document | What it covers |
|---|---|
| [docs/01-architecture.md](docs/01-architecture.md) | Topology, planes, delivery semantics, failure model |
| [docs/02-extraction-strategies.md](docs/02-extraction-strategies.md) | The five strategies, their mechanics and their bugs |
| [docs/03-source-families.md](docs/03-source-families.md) | Per-source approaches and trade-offs |
| [docs/04-security.md](docs/04-security.md) | Identity, encryption, non-exposure controls, threat model |
| [docs/05-audit-and-observability.md](docs/05-audit-and-observability.md) | Hash-chained ledger, tracing, metrics, reconciliation |
| [docs/06-performance-and-cost-guardrails.md](docs/06-performance-and-cost-guardrails.md) | Being a good tenant |
| [docs/07-implementation-roadmap.md](docs/07-implementation-roadmap.md) | Build order and what to negotiate at onboarding |

## Diagrams

| | |
|---|---|
| [Hybrid topology](docs/diagrams/d1_topology.svg) | [Control/data plane split](docs/diagrams/d2_planes.svg) |
| [Strategy decision tree](docs/diagrams/d3_strategy_decision.svg) | [Database CDC handover](docs/diagrams/d4_database_cdc.svg) |
| [Warehouse & lakehouse paths](docs/diagrams/d5_analytics_sources.svg) | [Collaboration & mailbox](docs/diagrams/d6_collaboration_sources.svg) |
| [Security architecture](docs/diagrams/d7_security.svg) | [Audit & delivery semantics](docs/diagrams/d8_audit_delivery.svg) |
| [Cost guardrails](docs/diagrams/d9_cost_guardrails.svg) | [Sink layer](docs/diagrams/d10_sink_layer.svg) |

Regenerate: `python3 docs/diagrams/render_diagrams.py` (needs `graphviz`).

---

## The shape of it

```
Provider network                              ID360 cloud
┌────────────────────────────┐                ┌──────────────────────┐
│  sources → ID360 AGENT ────┼── mTLS,        │  CONTROL PLANE       │
│              │             │   outbound     │  (FastAPI)           │
│              │             │   metadata only│  registry · policy   │
│              ▼             │───────────────▶│  state · audit       │
│           SINK             │                │  secret broker       │
└────────────────────────────┘                └──────────────────────┘
        bulk data never traverses the control plane
```

**Outbound-only from the provider.** No inbound firewall rule, no VPN. This is
what gets the design through a security review.

**The control plane never touches bulk data.** It carries task descriptors,
watermarks, schema fingerprints, and audit events. A compromise there leaks
metadata, not customer records — and it scales with job count, not data volume.

---

## The five strategies

Every connector resolves to one of these. Always take the cheapest the source
can support.

| | Strategy | Mechanism | Source load |
|---|---|---|---|
| 5 | Storage-native | Read Parquet via manifest, or engine `UNLOAD` to a stage | ~zero |
| 3 | Log CDC | WAL / binlog / LogMiner | ~zero on the query path |
| 4 | Change feed | Graph `/delta`, Drive `changes`, Iceberg snapshot diff | ~zero |
| 2 | Watermark | `WHERE (ts, pk) > (:hwm_ts, :hwm_pk)`, bounded above | low |
| 1 | Full snapshot | Read everything, partitioned, off a replica | highest |

Full snapshot is a bootstrap and reconciliation operation, never the steady
state. Landing there on a large table is a signal to go back to the provider —
the fix is cheap for them and the ongoing cost is not.

---

## Layout

```
docs/            architecture, per-source approaches, security, ops
  diagrams/      SVGs + the Graphviz script that generates them
src/id360_connect/
  core/          models · state · budget · audit · crypto · schema · retry · logging
  security/      secret broker (Vault / STS / Key Vault)
  sinks/         object store + Iceberg/Delta · RDBMS · Kafka
  connectors/    database · warehouse · lakehouse · lake · msgraph · gdrive · imap
  api/           FastAPI control plane · agent
tests/           invariant tests (31, all passing)
deploy/          docker-compose for local development
```

## Running

```bash
pip install -r requirements.txt

# Control plane
uvicorn id360_connect.api.control_plane:app --reload   # docs at /docs

# Tests
python3 -m pytest tests/ -q
```

---

## Non-negotiables

These are enforced by the framework, not left to connector authors, because
every one of them fails *silently* when it is left to good intentions.

1. **The position advances only after the sink commits.** Never the other way
   round. The most common correctness bug in hand-rolled connectors, and it
   loses data without erroring.
2. **Watermark windows are bounded above** at `now() - safety_lag`, with a
   composite `(ts, pk)` cursor.
3. **Masking and row filters are pushed into the source query.** If we may not
   hold a value we never fetch it. Fetch-then-drop is a leak with extra steps.
4. **No `SELECT *`.** The column list comes from the policy contract.
5. **Statement timeout on every query, with server-side cancel.** Dropping the
   connection leaves the provider paying for an orphaned query.
6. **`Retry-After` is honoured exactly.** Never substitute your own curve.
7. **Every extraction is priced before it runs.** `EXPLAIN` / `dry_run` /
   manifest sum, checked against the budget. Breach means: cancel at source,
   park the job, notify both sides.
8. **Row values never appear in logs.** Redaction runs in the formatter, so a
   careless call site cannot bypass it.
9. **CDC does not ship without slot-lag alerting.** An abandoned replication
   slot fills the primary's disk and takes down the provider's database.
10. **Audit denials as well as successes.** Auditors ask what you refused to
    read.

## What to negotiate before writing connector code

Cheap for the provider, expensive for you to work around later:

read replica · dedicated compute (warehouse / WLM queue / app registration) ·
explicit object allowlist with read-only grants · CDC access plus agreed lag
ownership · event notifications on lake buckets · blackout windows from their
calendar · an agreed byte budget with a hard stop · **a named technical contact
for 3 a.m.**

That last one is the one people forget and the one that matters most during the
first incident.
