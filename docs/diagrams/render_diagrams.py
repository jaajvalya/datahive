#!/usr/bin/env python3
"""Render all ID360 connector architecture diagrams to SVG using Graphviz.

Usage:  python3 render_diagrams.py [output_dir]
Requires: graphviz (system `dot` binary) and the `graphviz` python package.
"""
import shutil
import sys
import tempfile
from pathlib import Path
from graphviz import Digraph

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- palette ----
INK = "#1a1a2e"
MUTED = "#6b7280"
LINE = "#9ca3af"
CTRL = "#e0e7ff"   # control plane
CTRL_L = "#4f46e5"
DATA = "#d1fae5"   # data plane / agent
DATA_L = "#059669"
SRC = "#fef3c7"    # sources
SRC_L = "#d97706"
SINK = "#fce7f3"   # sinks
SINK_L = "#be185d"
SEC = "#fee2e2"    # security
SEC_L = "#dc2626"
NEUT = "#f3f4f6"

FONT = "Helvetica"


def base(name, rankdir="TB", size=None):
    g = Digraph(name, format="svg")
    g.attr(rankdir=rankdir, bgcolor="transparent", splines="spline",
           nodesep="0.45", ranksep="0.7", fontname=FONT, compound="true")
    if size:
        g.attr(size=size)
    g.attr("node", shape="box", style="rounded,filled", fontname=FONT,
           fontsize="11", color=LINE, fontcolor=INK, penwidth="1.4",
           margin="0.18,0.12")
    g.attr("edge", fontname=FONT, fontsize="9", color=LINE,
           fontcolor=MUTED, penwidth="1.2", arrowsize="0.75")
    return g


def cluster(g, name, label, fill, line):
    c = g.subgraph(name=f"cluster_{name}")
    return c


def write(g, filename):
    """Render via a scratch dir — some mounted filesystems disallow unlink,
    which breaks graphviz's cleanup step."""
    scratch = Path(tempfile.mkdtemp())
    try:
        produced = Path(g.render(scratch / filename, format="svg", cleanup=True))
        (OUT / f"{filename}.svg").write_bytes(produced.read_bytes())
        print(f"  wrote {filename}.svg")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# =============================================================== D1: topology
def d1_topology():
    g = base("topology", rankdir="LR")
    g.attr(label="ID360 — Hybrid Topology: On-Prem Agent, Cloud Control Plane",
           labelloc="t", fontsize="15", fontcolor=INK, fontname=FONT)

    with g.subgraph(name="cluster_provider") as c:
        c.attr(label="PROVIDER NETWORK  (on-prem DC or their cloud VPC)",
               style="rounded,filled", fillcolor="#fafafa", color=SRC_L,
               fontsize="11", fontcolor=SRC_L, penwidth="2")

        with c.subgraph(name="cluster_sources") as s:
            s.attr(label="Data sources", style="rounded,filled",
                   fillcolor=SRC, color=SRC_L, fontsize="10", fontcolor=SRC_L)
            s.node("db", "Databases\nOracle · SQL Server\nPostgres · Db2", fillcolor="#ffffff")
            s.node("wh", "Warehouse\nSnowflake · BigQuery\nRedshift · Teradata", fillcolor="#ffffff")
            s.node("lh", "Lakehouse / Lake\nDelta · Iceberg\nHDFS · S3 · ADLS", fillcolor="#ffffff")
            s.node("collab", "Collaboration\nSharePoint · OneDrive\nGoogle Drive · Mailbox", fillcolor="#ffffff")

        c.node("agent", "ID360 AGENT  (stateless)\n\n"
                        "• task executor + lease\n"
                        "• strategy resolver\n"
                        "• budget governor\n"
                        "• encrypted local WAL\n"
                        "• Arrow → Parquet encoder",
               fillcolor=DATA, color=DATA_L, penwidth="2", fontsize="11")

    with g.subgraph(name="cluster_id360") as c:
        c.attr(label="ID360 CLOUD", style="rounded,filled", fillcolor="#fafafa",
               color=CTRL_L, fontsize="11", fontcolor=CTRL_L, penwidth="2")
        c.node("cp", "CONTROL PLANE  (FastAPI)\n\n"
                     "• connection registry\n"
                     "• scheduler / job DAG\n"
                     "• policy engine\n"
                     "• schema registry\n"
                     "• watermark + state store\n"
                     "• audit ledger (hash-chained)\n"
                     "• secret broker",
               fillcolor=CTRL, color=CTRL_L, penwidth="2", fontsize="11")

    with g.subgraph(name="cluster_sink") as c:
        c.attr(label="SINK  (on-prem OR cloud — deployment choice)",
               style="rounded,filled", fillcolor="#fafafa", color=SINK_L,
               fontsize="11", fontcolor=SINK_L, penwidth="2")
        c.node("obj", "Object store + table format\nS3 / ADLS / MinIO\nIceberg · Delta", fillcolor=SINK, color=SINK_L)
        c.node("rdb", "Relational\nPostgres staging", fillcolor=SINK, color=SINK_L)
        c.node("stream", "Stream\nKafka topic", fillcolor=SINK, color=SINK_L)

    for n in ("db", "wh", "lh", "collab"):
        g.edge(n, "agent", color=SRC_L)

    g.edge("agent", "cp", label="  outbound only · mTLS\n  tasks, watermarks,\n  schema, metrics, audit\n  NO BULK DATA  ",
           color=CTRL_L, penwidth="2", style="dashed", dir="both")
    g.edge("agent", "obj", label="  bulk data\n  (never via control plane)  ", color=SINK_L, penwidth="2")
    g.edge("agent", "rdb", color=SINK_L, penwidth="2")
    g.edge("agent", "stream", color=SINK_L, penwidth="2")
    write(g, "d1_topology")


# ========================================================== D2: plane layering
def d2_planes():
    g = base("planes", rankdir="TB")
    g.attr(label="Control Plane / Data Plane Separation",
           labelloc="t", fontsize="15", fontcolor=INK)

    with g.subgraph(name="cluster_cp") as c:
        c.attr(label="CONTROL PLANE — small, structured, metadata only",
               style="rounded,filled", fillcolor=CTRL, color=CTRL_L,
               fontsize="11", fontcolor=CTRL_L, penwidth="2")
        c.node("api", "FastAPI\n/v1 REST + OpenAPI", fillcolor="#ffffff")
        c.node("reg", "Connection\nregistry", fillcolor="#ffffff")
        c.node("sched", "Scheduler\njob DAG · leases", fillcolor="#ffffff")
        c.node("pol", "Policy engine\nallowlist · masking\nrow filters · budgets", fillcolor="#ffffff")
        c.node("schema", "Schema registry\nfingerprint · drift", fillcolor="#ffffff")
        c.node("state", "State store\nwatermark · LSN\ndelta token", fillcolor="#ffffff")
        c.node("audit", "Audit ledger\nappend-only\nhash-chained", fillcolor="#ffffff")
        c.node("broker", "Secret broker\nshort-lived creds", fillcolor="#ffffff")

    with g.subgraph(name="cluster_dp") as c:
        c.attr(label="DATA PLANE — high volume, runs next to the source",
               style="rounded,filled", fillcolor=DATA, color=DATA_L,
               fontsize="11", fontcolor=DATA_L, penwidth="2")
        c.node("exec", "Task executor\nlease · heartbeat", fillcolor="#ffffff")
        c.node("resolver", "Strategy resolver\npicks S1–S5", fillcolor="#ffffff")
        c.node("reader", "Reader plugin\n(source-specific I/O only)", fillcolor="#ffffff")
        c.node("gov", "Budget governor\ntoken bucket:\nrows · bytes · qsec · calls", fillcolor="#ffffff")
        c.node("enc", "Encoder\nArrow → Parquet\n+ _id360 envelope", fillcolor="#ffffff")
        c.node("wal", "Encrypted WAL\nin-flight spool", fillcolor="#ffffff")
        c.node("writer", "Sink writer\npluggable", fillcolor="#ffffff")

    g.edge("api", "exec", label="  lease task  ", style="dashed", color=CTRL_L, dir="both")
    g.edge("pol", "gov", label="  budget + policy  ", style="dashed", color=CTRL_L)
    g.edge("state", "resolver", label="  resume position  ", style="dashed", color=CTRL_L)
    g.edge("exec", "audit", label="  events  ", style="dashed", color=CTRL_L)
    g.edge("enc", "schema", label="  fingerprint  ", style="dashed", color=CTRL_L)
    g.edge("broker", "reader", label="  15-min token  ", style="dashed", color=SEC_L)

    g.edge("exec", "resolver")
    g.edge("resolver", "reader")
    g.edge("reader", "gov")
    g.edge("gov", "enc")
    g.edge("enc", "wal")
    g.edge("wal", "writer")
    write(g, "d2_planes")


# ==================================================== D3: strategy decision tree
def d3_decision():
    g = base("decision", rankdir="TB")
    g.attr(label="Extraction Strategy Decision Tree  (always prefer the cheapest viable)",
           labelloc="t", fontsize="15", fontcolor=INK)
    g.attr("node", shape="box")

    dec = dict(shape="diamond", fillcolor=NEUT, style="filled", margin="0.05,0.02")
    g.node("q1", "Open table format\nor raw files readable\ndirectly?", **dec)
    g.node("q2", "Change feed with\ndurable cursor?", **dec)
    g.node("q3", "Log-level CDC granted\nAND lag ownership\naccepted?", **dec)
    g.node("q4", "Indexed monotonic\nchange column?", **dec)

    g.node("s5", "S5 · STORAGE-NATIVE\nread Parquet via manifest;\nincremental = snapshot diff\n\nengine cost: ZERO",
           fillcolor=DATA, color=DATA_L, penwidth="2")
    g.node("s4", "S4 · CHANGE FEED\nGraph /delta · Drive changes\nDelta CDF · Iceberg incremental\n\nsource cost: near-zero",
           fillcolor=DATA, color=DATA_L, penwidth="2")
    g.node("s3", "S3 · LOG CDC\nWAL · binlog · LogMiner\ncomplete incl. DELETEs\n\nquery-path cost: zero",
           fillcolor=DATA, color=DATA_L, penwidth="2")
    g.node("s2", "S2 · WATERMARK\ncomposite (ts, pk), bounded window\n+ periodic key-set diff for deletes",
           fillcolor=SRC, color=SRC_L, penwidth="2")
    g.node("s1", "S1 · FULL SNAPSHOT\npartitioned · off a replica\nmaintenance window only\n\nESCALATE to provider",
           fillcolor=SEC, color=SEC_L, penwidth="2")

    g.node("boot", "Bootstrap with S5b bulk UNLOAD\nwhere available, else S1 partitioned",
           fillcolor=NEUT, style="rounded,filled,dashed")

    g.edge("q1", "s5", label=" yes ", color=DATA_L)
    g.edge("q1", "q2", label=" no ")
    g.edge("q2", "s4", label=" yes ", color=DATA_L)
    g.edge("q2", "q3", label=" no ")
    g.edge("q3", "s3", label=" yes ", color=DATA_L)
    g.edge("q3", "q4", label=" no ")
    g.edge("q4", "s2", label=" yes ", color=SRC_L)
    g.edge("q4", "s1", label=" no ", color=SEC_L)
    g.edge("s4", "boot", style="dashed", color=MUTED)
    g.edge("s3", "boot", style="dashed", color=MUTED)
    g.edge("s2", "boot", style="dashed", color=MUTED)
    write(g, "d3_strategy_decision")


# ================================================= D4: database CDC handover
def d4_cdc():
    g = base("cdc", rankdir="TB")
    g.attr(label="Database Connector — Consistent Snapshot → CDC Stream Handover",
           labelloc="t", fontsize="15", fontcolor=INK)

    g.node("t0", "1 · Open log position P0\n(replication slot / binlog coord / SCN)\nSTART BUFFERING — do not apply yet",
           fillcolor=CTRL, color=CTRL_L)
    g.node("t1", "2 · Consistent snapshot AS OF P0\nrepeatable-read · no table locks\npartitioned by PK hash / range",
           fillcolor=SRC, color=SRC_L)
    g.node("t2", "3 · Emit snapshot rows\n_id360_op = 'R'\nposition = P0",
           fillcolor=DATA, color=DATA_L)
    g.node("t3", "4 · Replay buffered log P0 → now\napply upsert-by-PK\n_id360_op = I / U / D",
           fillcolor=DATA, color=DATA_L)
    g.node("t4", "5 · Steady state: tail the log\ncommit → advance position\nheartbeat on idle to bound slot lag",
           fillcolor=DATA, color=DATA_L, penwidth="2")

    g.node("guard1", "GUARD: position older than\nlog retention?\n→ invalidate, re-snapshot, ALERT\n(never silently resume from earliest)",
           fillcolor=SEC, color=SEC_L, shape="note", style="filled")
    g.node("guard2", "GUARD: slot lag > threshold\n→ page on-call\nan abandoned slot fills the\nprimary's WAL disk",
           fillcolor=SEC, color=SEC_L, shape="note", style="filled")
    g.node("nolock", "If P0 cannot be made causally\nconsistent with the snapshot:\nuse chunked low/high watermarks\n(DBLog) — zero locking",
           fillcolor=NEUT, shape="note", style="filled")

    g.edge("t0", "t1")
    g.edge("t1", "t2")
    g.edge("t2", "t3")
    g.edge("t3", "t4")
    g.edge("t4", "guard1", style="dashed", color=SEC_L)
    g.edge("t4", "guard2", style="dashed", color=SEC_L)
    g.edge("t1", "nolock", style="dashed", color=MUTED)
    write(g, "d4_database_cdc")


# ============================================= D5: warehouse / lakehouse paths
def d5_analytics():
    g = base("analytics", rankdir="LR")
    g.attr(label="Warehouse & Lakehouse — Zero-Copy and Bulk-Unload Paths",
           labelloc="t", fontsize="15", fontcolor=INK)

    with g.subgraph(name="cluster_bad") as c:
        c.attr(label="ANTI-PATTERN — do not do this", style="rounded,filled",
               fillcolor="#fff1f2", color=SEC_L, fontsize="10", fontcolor=SEC_L)
        c.node("bad1", "JDBC / ODBC cursor\nrow-by-row fetch", fillcolor="#ffffff")
        c.node("bad2", "Warehouse held HOT\nfor the whole transfer\n→ 10× the provider's bill", fillcolor=SEC, color=SEC_L)
        c.edge("bad1", "bad2", color=SEC_L)

    with g.subgraph(name="cluster_lh") as c:
        c.attr(label="PATH A — Lakehouse / Lake: read files directly (engine never runs)",
               style="rounded,filled", fillcolor="#f0fdf4", color=DATA_L,
               fontsize="10", fontcolor=DATA_L)
        c.node("meta", "Read table metadata\nIceberg manifest /\nDelta _delta_log", fillcolor="#ffffff")
        c.node("prune", "Partition + stats pruning\nresolve snapshot N", fillcolor="#ffffff")
        c.node("files", "GET only the needed\nParquet objects\nfrom S3 / ADLS / HDFS", fillcolor=DATA, color=DATA_L)
        c.node("diff", "INCREMENTAL:\ndiff file list\nsnapshot M → N", fillcolor=DATA, color=DATA_L)
        c.edge("meta", "prune")
        c.edge("prune", "files")
        c.edge("prune", "diff")

    with g.subgraph(name="cluster_wh") as c:
        c.attr(label="PATH B — Warehouse: engine-native bulk unload (short burst, then idle)",
               style="rounded,filled", fillcolor="#f0fdf4", color=DATA_L,
               fontsize="10", fontcolor=DATA_L)
        c.node("unload", "COPY INTO @stage (Snowflake)\nEXPORT DATA (BigQuery)\nUNLOAD PARQUET (Redshift)\nCETAS (Synapse)", fillcolor="#ffffff")
        c.node("stage", "Parquet files in\ncustomer-owned stage", fillcolor=DATA, color=DATA_L)
        c.node("pull", "Agent reads files\nat its own pace\n— warehouse now IDLE", fillcolor=DATA, color=DATA_L)
        c.edge("unload", "stage")
        c.edge("stage", "pull")

    with g.subgraph(name="cluster_c") as c:
        c.attr(label="PATH C — fallback: columnar result API",
               style="rounded,filled", fillcolor="#fffbeb", color=SRC_L,
               fontsize="10", fontcolor=SRC_L)
        c.node("arrow", "BigQuery Storage Read API\nDatabricks Cloud Fetch\nSnowflake Arrow · ADBC\n5–20× vs row protocol", fillcolor=SRC, color=SRC_L)

    g.node("sink", "ID360 sink", fillcolor=SINK, color=SINK_L, penwidth="2")
    g.edge("files", "sink", color=SINK_L)
    g.edge("diff", "sink", color=SINK_L)
    g.edge("pull", "sink", color=SINK_L)
    g.edge("arrow", "sink", color=SINK_L)
    write(g, "d5_analytics_sources")


# ========================================== D6: collaboration / mailbox sources
def d6_collab():
    g = base("collab", rankdir="LR")
    g.attr(label="SharePoint · OneDrive · Google Drive · Mailbox — Delta-Token Pattern",
           labelloc="t", fontsize="15", fontcolor=INK)

    with g.subgraph(name="cluster_auth") as c:
        c.attr(label="AUTH — app-only, least privilege, scoped",
               style="rounded,filled", fillcolor=SEC, color=SEC_L,
               fontsize="10", fontcolor=SEC_L)
        c.node("auth", "M365: client-credentials + certificate\n  Sites.Selected / Mail.Read (app-only)\n  + ApplicationAccessPolicy scoping\n\n"
                       "Google: domain-wide delegation OR\n  per-user OAuth · least scope\n\n"
                       "Tokens minted per run, 15-min TTL", fillcolor="#ffffff")

    g.node("delta", "1 · CHANGE DETECTION\nGraph  /delta  → deltaLink\nDrive changes.list → pageToken\nIMAP  UIDNEXT/MODSEQ (CONDSTORE)\nEWS/Graph mail /delta",
           fillcolor=CTRL, color=CTRL_L, penwidth="2")
    g.node("dedupe", "2 · DEDUPE + FILTER\ncollapse N edits of one doc → 1 fetch\napply policy allowlist:\nsites · drives · folders · labels\ndrop by MIME / size / age",
           fillcolor=NEUT)
    g.node("fetch", "3 · CONTENT FETCH  (the real cost)\nrange-GET large files · resume on 5xx\nMIME-part walk for mail\nattachments fetched separately\nrespect Retry-After EXACTLY",
           fillcolor=DATA, color=DATA_L)
    g.node("extract", "4 · EXTRACT + CLASSIFY\ntext + metadata + ACL snapshot\noptional OCR / parse\nPII classification → _id360_pii_class",
           fillcolor=DATA, color=DATA_L)
    g.node("commit", "5 · SINK COMMIT\nthen persist deltaLink / pageToken\nNEVER before",
           fillcolor=SINK, color=SINK_L, penwidth="2")

    g.node("resync", "TOKEN EXPIRY\n410 Gone / resyncRequired\n→ full re-enumeration\n→ ALERT (this is expensive)",
           fillcolor=SEC, color=SEC_L, shape="note", style="filled")
    g.node("throttle", "THROTTLING\nGraph: per-app + per-tenant buckets\nDrive: per-user QPS quota\nHonour Retry-After · halve concurrency\nNEVER retry-storm",
           fillcolor=SEC, color=SEC_L, shape="note", style="filled")
    g.node("acl", "ACL FIDELITY\ncapture permissions WITH content\nso downstream can enforce\nthe source's access model",
           fillcolor="#ede9fe", color="#7c3aed", shape="note", style="filled")

    g.edge("auth", "delta", style="dashed", color=SEC_L)
    g.edge("delta", "dedupe")
    g.edge("dedupe", "fetch")
    g.edge("fetch", "extract")
    g.edge("extract", "commit")
    g.edge("delta", "resync", style="dashed", color=SEC_L)
    g.edge("fetch", "throttle", style="dashed", color=SEC_L)
    g.edge("extract", "acl", style="dashed", color="#7c3aed")
    write(g, "d6_collaboration_sources")


# ============================================================== D7: security
def d7_security():
    g = base("security", rankdir="TB")
    g.attr(label="Security Architecture — Trust Boundaries, Keys, and Non-Exposure",
           labelloc="t", fontsize="15", fontcolor=INK)

    with g.subgraph(name="cluster_identity") as c:
        c.attr(label="IDENTITY — no long-lived secrets anywhere",
               style="rounded,filled", fillcolor=SEC, color=SEC_L,
               fontsize="10", fontcolor=SEC_L)
        c.node("spiffe", "Agent identity\nSPIFFE/SVID or\nworkload federation\n(no static agent key)", fillcolor="#ffffff")
        c.node("vault", "Tenant's OWN vault\nHashiCorp / AWS SM /\nAzure KV / GCP SM", fillcolor="#ffffff")
        c.node("mint", "Secret broker mints\nshort-lived source creds\nTTL ≤ 15 min\nscoped to one task", fillcolor="#ffffff")
        c.node("noref", "Control plane stores\nREFERENCES only\n— never secret values", fillcolor="#ffffff")

    with g.subgraph(name="cluster_transit") as c:
        c.attr(label="IN TRANSIT", style="rounded,filled", fillcolor="#dbeafe",
               color="#2563eb", fontsize="10", fontcolor="#2563eb")
        c.node("mtls", "Agent ↔ control plane\nmTLS 1.3 · cert pinning\noutbound-only", fillcolor="#ffffff")
        c.node("srctls", "Agent ↔ source\nTLS required · verify chain\nrefuse downgrade", fillcolor="#ffffff")
        c.node("sinktls", "Agent ↔ sink\nTLS + SigV4 / SAS / IAM", fillcolor="#ffffff")

    with g.subgraph(name="cluster_rest") as c:
        c.attr(label="AT REST", style="rounded,filled", fillcolor="#d1fae5",
               color=DATA_L, fontsize="10", fontcolor=DATA_L)
        c.node("envelope", "Envelope encryption\nDEK per batch (AES-256-GCM)\nwrapped by tenant KEK in KMS\ntenant can revoke → data dark", fillcolor="#ffffff")
        c.node("walenc", "Agent local WAL\nencrypted, ephemeral\nshredded on commit", fillcolor="#ffffff")
        c.node("sinkenc", "Sink: SSE-KMS / CMK\n+ object lock on audit\nbucket-level deny on public", fillcolor="#ffffff")

    with g.subgraph(name="cluster_nonexp") as c:
        c.attr(label="NON-EXPOSURE CONTROLS", style="rounded,filled",
               fillcolor="#ede9fe", color="#7c3aed", fontsize="10", fontcolor="#7c3aed")
        c.node("mask", "Column masking + row filters\napplied AT THE SOURCE QUERY\n— never fetch then drop", fillcolor="#ffffff")
        c.node("noplog", "Structured logs, allowlisted fields\nno row values · no query params\nsecrets redacted at formatter", fillcolor="#ffffff")
        c.node("noerr", "Error messages sanitized\nsource errors never bubble\nverbatim to API consumers", fillcolor="#ffffff")
        c.node("egress", "Agent egress allowlist\nonly control plane + sink + source\nDNS pinning", fillcolor="#ffffff")

    g.edge("spiffe", "mint")
    g.edge("vault", "mint")
    g.edge("mint", "noref", style="dashed")
    g.edge("mint", "srctls", style="dashed", color=SEC_L)
    g.edge("mtls", "envelope", style="invis")
    g.edge("envelope", "walenc")
    g.edge("envelope", "sinkenc")
    g.edge("mask", "noplog", style="invis")
    write(g, "d7_security")


# ================================================== D8: audit + exactly-once
def d8_audit():
    g = base("audit", rankdir="TB")
    g.attr(label="Delivery Semantics & Audit Ledger — Idempotent Commit",
           labelloc="t", fontsize="15", fontcolor=INK)

    g.node("a1", "1 · LEASE\nagent leases task\ncontrol plane records:\nagent SVID · TTL · policy version",
           fillcolor=CTRL, color=CTRL_L)
    g.node("a2", "2 · AUTHORIZE\npolicy engine evaluates\nobject allowlist · masking ·\nrow filter · byte budget\n→ DENY is also an audit event",
           fillcolor=SEC, color=SEC_L)
    g.node("a3", "3 · READ batch N\ninto encrypted local WAL\nrow count + hash accumulated",
           fillcolor=DATA, color=DATA_L)
    g.node("a4", "4 · WRITE to sink\ndeterministic path:\n{table}/{run_id}/{batch_id}.parquet\n→ replay overwrites byte-identically",
           fillcolor=SINK, color=SINK_L)
    g.node("a5", "5 · COMMIT (atomic, control plane)\n advance watermark\n+ append audit record\n+ mark batch durable\nALL OR NOTHING",
           fillcolor=CTRL, color=CTRL_L, penwidth="2")
    g.node("a6", "6 · SHRED WAL segment",
           fillcolor=NEUT)

    g.node("crash", "CRASH ANYWHERE BEFORE 5\n→ lease expires\n→ another agent replays from\n   last committed checkpoint\n→ idempotent: same path,\n   same batch_id, MERGE on batch_id",
           fillcolor="#fff1f2", color=SEC_L, shape="note", style="filled")

    g.node("ledger", "AUDIT LEDGER (append-only)\n\nrecord = {run_id, batch_id, actor_svid,\n source_id, object, strategy, predicate_hash,\n position_from, position_to, row_count, byte_count,\n schema_ver, policy_ver, outcome, ts}\n\nprev_hash → hash chain → tamper-evident\nWORM storage · object lock · retention hold",
           fillcolor="#ede9fe", color="#7c3aed", penwidth="2")

    g.node("answers", "The ledger answers, per row:\n• WHO pulled it (agent SVID + human owner)\n• WHEN, from WHERE, under WHICH policy\n• WHICH predicate selected it\n• WHERE it landed\n• WAS it masked, and by which rule",
           shape="note", style="filled", fillcolor=NEUT)

    for a, b in [("a1", "a2"), ("a2", "a3"), ("a3", "a4"), ("a4", "a5"), ("a5", "a6")]:
        g.edge(a, b)
    g.edge("a3", "crash", style="dashed", color=SEC_L)
    g.edge("a5", "ledger", style="dashed", color="#7c3aed", penwidth="2")
    g.edge("a2", "ledger", style="dashed", color="#7c3aed")
    g.edge("ledger", "answers", style="dashed", color=MUTED)
    write(g, "d8_audit_delivery")


# ============================================== D9: cost & performance governor
def d9_governor():
    g = base("governor", rankdir="LR")
    g.attr(label="Being a Good Tenant — Cost & Performance Guardrails",
           labelloc="t", fontsize="15", fontcolor=INK)

    with g.subgraph(name="cluster_pre") as c:
        c.attr(label="BEFORE the read", style="rounded,filled", fillcolor=CTRL,
               color=CTRL_L, fontsize="10", fontcolor=CTRL_L)
        c.node("p1", "EXPLAIN / dry-run\nestimate bytes + cost\nrefuse if > budget", fillcolor="#ffffff")
        c.node("p2", "Blackout windows\nno reads during the\nprovider's batch window\nor business peak", fillcolor="#ffffff")
        c.node("p3", "Predicate + column pushdown\nnever SELECT *\nproject only what the\ncontract requires", fillcolor="#ffffff")
        c.node("p4", "Dedicated small warehouse /\nresource group / replica\nnever the prod primary", fillcolor="#ffffff")

    with g.subgraph(name="cluster_during") as c:
        c.attr(label="DURING the read", style="rounded,filled", fillcolor=DATA,
               color=DATA_L, fontsize="10", fontcolor=DATA_L)
        c.node("d1", "Token buckets\nrows/s · bytes/s\nqueries/min · API calls/min\nper source AND per object", fillcolor="#ffffff")
        c.node("d2", "Adaptive concurrency\nAIMD: grow on success,\nHALVE on 429/503/timeout", fillcolor="#ffffff")
        c.node("d3", "Statement timeout on\nEVERY query\n+ server-side cancel\n(no orphaned queries)", fillcolor="#ffffff")
        c.node("d4", "Chunked partitions\nsplit-on-fail:\ntoo big → halve → retry", fillcolor="#ffffff")
        c.node("d5", "Circuit breaker\nper (source, object)\none hot table cannot take\ndown the connection", fillcolor="#ffffff")

    with g.subgraph(name="cluster_after") as c:
        c.attr(label="AFTER / CONTINUOUS", style="rounded,filled", fillcolor=SINK,
               color=SINK_L, fontsize="10", fontcolor=SINK_L)
        c.node("f1", "Cost attribution\nquery tags · job labels\nso the provider can SEE\nexactly what you cost them", fillcolor="#ffffff")
        c.node("f2", "Shared consumption report\nbytes · query-seconds ·\nAPI calls · $ estimate\ndelivered to the provider", fillcolor="#ffffff")
        c.node("f3", "Right-size the schedule\nif 95% of runs return\n0 rows → back off", fillcolor="#ffffff")

    g.node("hard", "HARD STOP\nbudget breach →\ncancel at source, park job,\nnotify BOTH sides.\nNever silently exceed.",
           fillcolor=SEC, color=SEC_L, penwidth="2")

    g.edge("p1", "d1", style="invis")
    g.edge("d1", "hard", color=SEC_L)
    g.edge("d2", "hard", color=SEC_L, style="dashed")
    g.edge("d1", "f1", style="invis")
    g.edge("f2", "f3", style="dashed", color=MUTED)
    write(g, "d9_cost_guardrails")


# ============================================================ D10: sink layer
def d10_sink():
    g = base("sink", rankdir="TB")
    g.attr(label="Pluggable Sink Layer — one writer contract, three backends",
           labelloc="t", fontsize="15", fontcolor=INK)

    g.node("iface", "Sink protocol\n\nopen(run) →\nwrite_batch(RecordBatch, meta) →\ncommit(batch_id) / abort()\n\nMUST be idempotent on batch_id",
           fillcolor=CTRL, color=CTRL_L, penwidth="2")

    g.node("obj", "ObjectStoreSink\nParquet + Iceberg/Delta\nS3 · ADLS · GCS · MinIO · HDFS",
           fillcolor=SINK, color=SINK_L)
    g.node("rdb", "RdbmsSink\nPostgres / any DBAPI",
           fillcolor=SINK, color=SINK_L)
    g.node("kafka", "StreamSink\nKafka topic",
           fillcolor=SINK, color=SINK_L)

    g.node("objd", "commit = table-format\nACID transaction\npartial run invisible to readers\nsnapshot isolation for free",
           shape="note", style="filled", fillcolor=NEUT)
    g.node("rdbd", "COPY into staging table\n→ MERGE on PK keyed by\n   _id360_batch_id\n→ atomic swap for full refresh",
           shape="note", style="filled", fillcolor=NEUT)
    g.node("kafkad", "key = PK for compaction\nidempotent producer\ntransactional commit\nheaders carry _id360 envelope",
           shape="note", style="filled", fillcolor=NEUT)

    g.node("layout", "Layout convention\n\n{root}/{source_id}/{object}/\n  _run={run_id}/\n  {batch_id}.parquet\n\ndeterministic → replay-safe\npartitioned by _id360_extract_ts date",
           fillcolor=DATA, color=DATA_L, shape="note", style="filled")

    g.edge("iface", "obj")
    g.edge("iface", "rdb")
    g.edge("iface", "kafka")
    g.edge("obj", "objd", style="dashed", color=MUTED)
    g.edge("rdb", "rdbd", style="dashed", color=MUTED)
    g.edge("kafka", "kafkad", style="dashed", color=MUTED)
    g.edge("obj", "layout", style="dashed", color=DATA_L)
    write(g, "d10_sink_layer")


if __name__ == "__main__":
    print(f"Rendering diagrams to {OUT}")
    d1_topology()
    d2_planes()
    d3_decision()
    d4_cdc()
    d5_analytics()
    d6_collab()
    d7_security()
    d8_audit()
    d9_governor()
    d10_sink()
    print("done.")
