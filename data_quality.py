"""Basic data-quality profiling for Governance → Data Quality.

Runs read-only SQL against Postgres or Snowflake for selected tables and
returns per-check results, issue cards, and a weighted DQ score.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

log = logging.getLogger("datahive.data_quality")

# Score weights (must sum to 1.0)
WEIGHTS = {
    "completeness": 0.35,
    "uniqueness": 0.25,
    "validity": 0.20,
    "schema": 0.10,
    "volume": 0.10,
}

NULL_WARN_PCT = 20.0
NULL_FAIL_PCT = 50.0
EMPTY_WARN_PCT = 10.0
DUP_FAIL_PCT = 1.0
MAX_PROFILE_COLS = 24
FRESHNESS_COL_RE = re.compile(
    r"(loaded_at|updated_at|modified_at|ingestion_ts|_dh_loaded_at|last_modified)",
    re.I,
)


class DataQualityError(Exception):
    def __init__(self, message: str, *, error_type: str = "validation") -> None:
        super().__init__(message)
        self.error_type = error_type


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sf_qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _norm_type(t: str) -> str:
    return str(t or "").lower()


def _is_text(t: str) -> bool:
    s = _norm_type(t)
    return any(x in s for x in ("char", "text", "string", "varchar"))


def _is_numeric(t: str) -> bool:
    s = _norm_type(t)
    return any(
        x in s
        for x in ("int", "numeric", "decimal", "float", "double", "real", "number", "bigint")
    )


def score_logic_docs() -> dict[str, Any]:
    return {
        "formula": (
            "DQ Score = 0.35×Completeness + 0.25×Uniqueness + 0.20×Validity "
            "+ 0.10×Schema + 0.10×Volume"
        ),
        "weights": WEIGHTS,
        "dimensions": {
            "completeness": (
                "100 − average null%% across profiled columns "
                f"(warn ≥{NULL_WARN_PCT:g}%%, fail ≥{NULL_FAIL_PCT:g}%%)."
            ),
            "uniqueness": (
                "100 − duplicate%% on primary key (or full-row duplicates when no PK). "
                f"Fail when duplicate rate ≥{DUP_FAIL_PCT:g}%%."
            ),
            "validity": (
                "100 − empty-string%% on text columns "
                f"(warn ≥{EMPTY_WARN_PCT:g}%%)."
            ),
            "schema": "100 if a primary key exists; 60 if columns exist but no PK; 0 if no columns.",
            "volume": "100 if row_count > 0; otherwise 0.",
        },
        "grades": {
            "A": "90–100",
            "B": "75–89",
            "C": "60–74",
            "D": "40–59",
            "F": "0–39",
        },
    }


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _run_sql(
    sql: str,
    execute: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    result = execute(sql)
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({cols[i]: row[i] if i < len(row) else None for i in range(len(cols))})
    return out


def _scalar(sql: str, execute: Callable[[str], dict[str, Any]], key: str | None = None) -> Any:
    rows = _run_sql(sql, execute)
    if not rows:
        return None
    if key:
        return rows[0].get(key)
    # first value
    return next(iter(rows[0].values()), None)


def _profile_table_postgres(
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
    pk_cols: list[str],
    execute: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    sch = _qi(schema)
    tbl = _qi(table)
    fq = f"{sch}.{tbl}"

    row_count = int(_scalar(f"SELECT COUNT(*) AS c FROM {fq}", execute, "c") or 0)

    profile_cols = columns[:MAX_PROFILE_COLS]
    null_stats: list[dict[str, Any]] = []
    empty_stats: list[dict[str, Any]] = []

    if row_count > 0 and profile_cols:
        parts = []
        for c in profile_cols:
            n = c["name"]
            qn = _qi(n)
            parts.append(f"SUM(CASE WHEN {qn} IS NULL THEN 1 ELSE 0 END) AS {_qi(n + '__nulls')}")
            if _is_text(c.get("type", "")):
                parts.append(
                    f"SUM(CASE WHEN {qn} IS NOT NULL AND BTRIM(CAST({qn} AS text)) = '' "
                    f"THEN 1 ELSE 0 END) AS {_qi(n + '__empty')}"
                )
        sql = f"SELECT {', '.join(parts)} FROM {fq}"
        agg = _run_sql(sql, execute)
        vals = agg[0] if agg else {}
        for c in profile_cols:
            n = c["name"]
            nulls = int(vals.get(n + "__nulls") or 0)
            pct = round(100.0 * nulls / row_count, 2) if row_count else 0.0
            null_stats.append(
                {
                    "column": n,
                    "nulls": nulls,
                    "null_pct": pct,
                    "nullable": bool(c.get("nullable", True)),
                    "type": c.get("type"),
                }
            )
            if _is_text(c.get("type", "")):
                empties = int(vals.get(n + "__empty") or 0)
                epct = round(100.0 * empties / row_count, 2) if row_count else 0.0
                empty_stats.append(
                    {"column": n, "empty": empties, "empty_pct": epct, "type": c.get("type")}
                )

    dup_pk = 0
    dup_pk_pct = 0.0
    if row_count > 0 and pk_cols:
        pk_list = ", ".join(_qi(c) for c in pk_cols)
        null_filter = " AND ".join(f"{_qi(c)} IS NOT NULL" for c in pk_cols)
        dup_pk = int(
            _scalar(
                f"""
                SELECT COALESCE(SUM(cnt - 1), 0) AS dups FROM (
                  SELECT {pk_list}, COUNT(*) AS cnt
                  FROM {fq}
                  WHERE {null_filter}
                  GROUP BY {pk_list}
                  HAVING COUNT(*) > 1
                ) x
                """,
                execute,
                "dups",
            )
            or 0
        )
        dup_pk_pct = round(100.0 * dup_pk / row_count, 4)

    dup_rows = 0
    dup_rows_pct = 0.0
    if row_count > 0 and not pk_cols and profile_cols:
        # Approximate full-row duplicates via hash of first N columns.
        exprs = ", ".join(f"COALESCE(CAST({_qi(c['name'])} AS text),'∅')" for c in profile_cols[:12])
        dup_rows = int(
            _scalar(
                f"""
                SELECT COALESCE(SUM(cnt - 1), 0) AS dups FROM (
                  SELECT md5(CONCAT_WS('|', {exprs})) AS h, COUNT(*) AS cnt
                  FROM {fq}
                  GROUP BY 1
                  HAVING COUNT(*) > 1
                ) x
                """,
                execute,
                "dups",
            )
            or 0
        )
        dup_rows_pct = round(100.0 * dup_rows / row_count, 4)

    freshness = None
    fresh_col = next(
        (c["name"] for c in columns if FRESHNESS_COL_RE.search(c["name"])),
        None,
    )
    if fresh_col and row_count > 0:
        qn = _qi(fresh_col)
        freshness = _run_sql(
            f"SELECT MAX({qn}) AS max_ts, MIN({qn}) AS min_ts, "
            f"COUNT(*) FILTER (WHERE {qn} IS NULL) AS null_ts FROM {fq}",
            execute,
        )
        freshness = freshness[0] if freshness else None
        if freshness:
            freshness = {"column": fresh_col, **freshness}

    return {
        "row_count": row_count,
        "null_stats": null_stats,
        "empty_stats": empty_stats,
        "dup_pk": dup_pk,
        "dup_pk_pct": dup_pk_pct,
        "dup_rows": dup_rows,
        "dup_rows_pct": dup_rows_pct,
        "pk_cols": pk_cols,
        "freshness": freshness,
        "column_count": len(columns),
    }


def _profile_table_snowflake(
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
    pk_cols: list[str],
    execute: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    # schema may be DB.SCHEMA
    parts = [p for p in str(schema).split(".") if p]
    if len(parts) >= 2:
        fq = f"{_sf_qi(parts[0])}.{_sf_qi(parts[1])}.{_sf_qi(table)}"
    else:
        fq = f"{_sf_qi(schema)}.{_sf_qi(table)}"

    row_count = int(_scalar(f"SELECT COUNT(*) AS C FROM {fq}", execute, "C") or 0)
    # Snowflake may uppercase aliases
    if row_count == 0:
        row_count = int(_scalar(f"SELECT COUNT(*) AS c FROM {fq}", execute, "c") or 0)

    profile_cols = columns[:MAX_PROFILE_COLS]
    null_stats: list[dict[str, Any]] = []
    empty_stats: list[dict[str, Any]] = []

    if row_count > 0 and profile_cols:
        parts_sql = []
        for c in profile_cols:
            n = c["name"]
            qn = _sf_qi(n)
            alias_n = f"N_{abs(hash(n)) % 10_000_000}"
            parts_sql.append(f"SUM(IFF({qn} IS NULL, 1, 0)) AS {alias_n}_NULLS")
            if _is_text(c.get("type", "")):
                parts_sql.append(
                    f"SUM(IFF({qn} IS NOT NULL AND TRIM(TO_VARCHAR({qn})) = '', 1, 0)) "
                    f"AS {alias_n}_EMPTY"
                )
        sql = f"SELECT {', '.join(parts_sql)} FROM {fq}"
        agg = _run_sql(sql, execute)
        vals = {str(k).upper(): v for k, v in (agg[0] if agg else {}).items()}
        for c in profile_cols:
            n = c["name"]
            alias_n = f"N_{abs(hash(n)) % 10_000_000}"
            nulls = int(vals.get(f"{alias_n}_NULLS".upper()) or 0)
            pct = round(100.0 * nulls / row_count, 2) if row_count else 0.0
            null_stats.append(
                {
                    "column": n,
                    "nulls": nulls,
                    "null_pct": pct,
                    "nullable": bool(c.get("nullable", True)),
                    "type": c.get("type"),
                }
            )
            if _is_text(c.get("type", "")):
                empties = int(vals.get(f"{alias_n}_EMPTY".upper()) or 0)
                epct = round(100.0 * empties / row_count, 2) if row_count else 0.0
                empty_stats.append(
                    {"column": n, "empty": empties, "empty_pct": epct, "type": c.get("type")}
                )

    dup_pk = 0
    dup_pk_pct = 0.0
    if row_count > 0 and pk_cols:
        pk_list = ", ".join(_sf_qi(c) for c in pk_cols)
        null_filter = " AND ".join(f"{_sf_qi(c)} IS NOT NULL" for c in pk_cols)
        dup_pk = int(
            _scalar(
                f"""
                SELECT COALESCE(SUM(cnt - 1), 0) AS DUPS FROM (
                  SELECT {pk_list}, COUNT(*) AS cnt
                  FROM {fq}
                  WHERE {null_filter}
                  GROUP BY {pk_list}
                  HAVING COUNT(*) > 1
                )
                """,
                execute,
                "DUPS",
            )
            or _scalar(
                f"""
                SELECT COALESCE(SUM(cnt - 1), 0) AS dups FROM (
                  SELECT {pk_list}, COUNT(*) AS cnt
                  FROM {fq}
                  WHERE {null_filter}
                  GROUP BY {pk_list}
                  HAVING COUNT(*) > 1
                )
                """,
                execute,
                "dups",
            )
            or 0
        )
        dup_pk_pct = round(100.0 * dup_pk / row_count, 4)

    return {
        "row_count": row_count,
        "null_stats": null_stats,
        "empty_stats": empty_stats,
        "dup_pk": dup_pk,
        "dup_pk_pct": dup_pk_pct,
        "dup_rows": 0,
        "dup_rows_pct": 0.0,
        "pk_cols": pk_cols,
        "freshness": None,
        "column_count": len(columns),
    }


def _build_checks_and_score(profile: dict[str, Any], table: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    row_count = int(profile.get("row_count") or 0)
    pk_cols = list(profile.get("pk_cols") or [])
    null_stats = list(profile.get("null_stats") or [])
    empty_stats = list(profile.get("empty_stats") or [])

    # Volume
    if row_count > 0:
        checks.append(
            {
                "id": "volume_nonempty",
                "dimension": "volume",
                "name": "Table has rows",
                "status": "pass",
                "severity": "info",
                "detail": f"{row_count:,} rows",
            }
        )
        volume_score = 100.0
    else:
        checks.append(
            {
                "id": "volume_nonempty",
                "dimension": "volume",
                "name": "Table has rows",
                "status": "fail",
                "severity": "critical",
                "detail": "Table is empty (0 rows)",
            }
        )
        issues.append(
            {
                "severity": "critical",
                "category": "volume",
                "table": table,
                "title": "Empty table",
                "detail": f"{table} has 0 rows.",
            }
        )
        volume_score = 0.0

    # Schema
    if pk_cols:
        schema_score = 100.0
        checks.append(
            {
                "id": "schema_pk",
                "dimension": "schema",
                "name": "Primary key defined",
                "status": "pass",
                "severity": "info",
                "detail": ", ".join(pk_cols),
            }
        )
    elif profile.get("column_count"):
        schema_score = 60.0
        checks.append(
            {
                "id": "schema_pk",
                "dimension": "schema",
                "name": "Primary key defined",
                "status": "warn",
                "severity": "medium",
                "detail": "No primary key — uniqueness checks use row hashing when available",
            }
        )
        issues.append(
            {
                "severity": "medium",
                "category": "schema",
                "table": table,
                "title": "Missing primary key",
                "detail": f"{table} has no PK constraint.",
            }
        )
    else:
        schema_score = 0.0
        checks.append(
            {
                "id": "schema_pk",
                "dimension": "schema",
                "name": "Primary key defined",
                "status": "fail",
                "severity": "high",
                "detail": "No columns discovered",
            }
        )

    # Completeness
    if row_count <= 0:
        completeness_score = 0.0
    elif null_stats:
        avg_null = sum(float(s["null_pct"]) for s in null_stats) / len(null_stats)
        completeness_score = max(0.0, min(100.0, 100.0 - avg_null))
        for s in null_stats:
            pct = float(s["null_pct"])
            if not s.get("nullable", True) and pct > 0:
                checks.append(
                    {
                        "id": f"null_nn_{s['column']}",
                        "dimension": "completeness",
                        "name": f"NOT NULL violations · {s['column']}",
                        "status": "fail",
                        "severity": "high",
                        "detail": f"{pct}% null ({s['nulls']:,} rows)",
                    }
                )
                issues.append(
                    {
                        "severity": "high",
                        "category": "completeness",
                        "table": table,
                        "title": f"Nulls in non-nullable column {s['column']}",
                        "detail": f"{pct}% null ({s['nulls']:,} rows)",
                    }
                )
            elif pct >= NULL_FAIL_PCT:
                checks.append(
                    {
                        "id": f"null_high_{s['column']}",
                        "dimension": "completeness",
                        "name": f"High null rate · {s['column']}",
                        "status": "fail",
                        "severity": "high",
                        "detail": f"{pct}% null (threshold {NULL_FAIL_PCT:g}%)",
                    }
                )
                issues.append(
                    {
                        "severity": "high",
                        "category": "completeness",
                        "table": table,
                        "title": f"High nulls in {s['column']}",
                        "detail": f"{pct}% null",
                    }
                )
            elif pct >= NULL_WARN_PCT:
                checks.append(
                    {
                        "id": f"null_warn_{s['column']}",
                        "dimension": "completeness",
                        "name": f"Elevated null rate · {s['column']}",
                        "status": "warn",
                        "severity": "medium",
                        "detail": f"{pct}% null (threshold {NULL_WARN_PCT:g}%)",
                    }
                )
                issues.append(
                    {
                        "severity": "medium",
                        "category": "completeness",
                        "table": table,
                        "title": f"Elevated nulls in {s['column']}",
                        "detail": f"{pct}% null",
                    }
                )
        checks.append(
            {
                "id": "completeness_avg",
                "dimension": "completeness",
                "name": "Average completeness",
                "status": "pass" if completeness_score >= 80 else ("warn" if completeness_score >= 60 else "fail"),
                "severity": "info",
                "detail": f"avg null {avg_null:.2f}% → score {completeness_score:.1f}",
            }
        )
    else:
        completeness_score = 100.0 if row_count > 0 else 0.0

    # Uniqueness
    dup_pk = int(profile.get("dup_pk") or 0)
    dup_pk_pct = float(profile.get("dup_pk_pct") or 0)
    dup_rows = int(profile.get("dup_rows") or 0)
    dup_rows_pct = float(profile.get("dup_rows_pct") or 0)
    if row_count <= 0:
        uniqueness_score = 0.0
        checks.append(
            {
                "id": "uniq_skipped",
                "dimension": "uniqueness",
                "name": "Uniqueness",
                "status": "fail",
                "severity": "info",
                "detail": "Skipped — table is empty",
            }
        )
    elif pk_cols:
        uniqueness_score = max(0.0, min(100.0, 100.0 - min(dup_pk_pct * 10, 100.0)))
        if dup_pk > 0:
            sev = "critical" if dup_pk_pct >= DUP_FAIL_PCT else "high"
            checks.append(
                {
                    "id": "uniq_pk",
                    "dimension": "uniqueness",
                    "name": "Primary key uniqueness",
                    "status": "fail",
                    "severity": sev,
                    "detail": f"{dup_pk:,} duplicate key rows ({dup_pk_pct}%)",
                }
            )
            issues.append(
                {
                    "severity": sev,
                    "category": "uniqueness",
                    "table": table,
                    "title": "Duplicate primary keys",
                    "detail": f"{dup_pk:,} duplicate key rows ({dup_pk_pct}%) on {', '.join(pk_cols)}",
                }
            )
        else:
            checks.append(
                {
                    "id": "uniq_pk",
                    "dimension": "uniqueness",
                    "name": "Primary key uniqueness",
                    "status": "pass",
                    "severity": "info",
                    "detail": "No duplicate PK values",
                }
            )
    else:
        uniqueness_score = max(0.0, min(100.0, 100.0 - min(dup_rows_pct * 10, 100.0)))
        if dup_rows > 0:
            checks.append(
                {
                    "id": "uniq_rows",
                    "dimension": "uniqueness",
                    "name": "Approximate row uniqueness",
                    "status": "warn" if dup_rows_pct < DUP_FAIL_PCT else "fail",
                    "severity": "medium",
                    "detail": f"~{dup_rows:,} duplicate rows ({dup_rows_pct}%)",
                }
            )
            issues.append(
                {
                    "severity": "medium",
                    "category": "uniqueness",
                    "table": table,
                    "title": "Duplicate rows detected",
                    "detail": f"~{dup_rows:,} duplicate rows ({dup_rows_pct}%)",
                }
            )
        else:
            checks.append(
                {
                    "id": "uniq_rows",
                    "dimension": "uniqueness",
                    "name": "Approximate row uniqueness",
                    "status": "pass",
                    "severity": "info",
                    "detail": "No hashed row duplicates in profiled columns",
                }
            )

    # Validity (empty strings)
    if row_count <= 0:
        validity_score = 0.0
    elif empty_stats:
        avg_empty = sum(float(s["empty_pct"]) for s in empty_stats) / max(len(empty_stats), 1)
        validity_score = max(0.0, min(100.0, 100.0 - avg_empty))
        for s in empty_stats:
            if float(s["empty_pct"]) >= EMPTY_WARN_PCT:
                checks.append(
                    {
                        "id": f"empty_{s['column']}",
                        "dimension": "validity",
                        "name": f"Empty strings · {s['column']}",
                        "status": "warn",
                        "severity": "medium",
                        "detail": f"{s['empty_pct']}% empty ({s['empty']:,} rows)",
                    }
                )
                issues.append(
                    {
                        "severity": "medium",
                        "category": "validity",
                        "table": table,
                        "title": f"Empty strings in {s['column']}",
                        "detail": f"{s['empty_pct']}% empty",
                    }
                )
        checks.append(
            {
                "id": "validity_avg",
                "dimension": "validity",
                "name": "Text validity (non-empty)",
                "status": "pass" if validity_score >= 90 else "warn",
                "severity": "info",
                "detail": f"avg empty {avg_empty:.2f}% → score {validity_score:.1f}",
            }
        )
    else:
        validity_score = 100.0 if row_count > 0 else 0.0

    # Freshness informational
    freshness = profile.get("freshness")
    if freshness and freshness.get("max_ts") is not None:
        checks.append(
            {
                "id": "freshness",
                "dimension": "volume",
                "name": f"Freshness · {freshness.get('column')}",
                "status": "pass",
                "severity": "info",
                "detail": f"max={freshness.get('max_ts')} · min={freshness.get('min_ts')}",
            }
        )

    dimensions = {
        "completeness": round(completeness_score, 1),
        "uniqueness": round(uniqueness_score, 1),
        "validity": round(validity_score, 1),
        "schema": round(schema_score, 1),
        "volume": round(volume_score, 1),
    }
    overall = round(
        sum(dimensions[k] * WEIGHTS[k] for k in WEIGHTS),
        1,
    )
    return {
        "score": overall,
        "grade": _grade(overall),
        "dimensions": dimensions,
        "checks": checks,
        "issues": issues,
        "profile": {
            "row_count": row_count,
            "column_count": profile.get("column_count"),
            "pk_cols": pk_cols,
            "null_stats": null_stats,
            "empty_stats": empty_stats,
            "dup_pk": dup_pk,
            "dup_pk_pct": dup_pk_pct,
            "dup_rows": dup_rows,
            "dup_rows_pct": dup_rows_pct,
            "freshness": freshness,
        },
    }


def run_data_quality(
    *,
    connector_id: str,
    schema: str,
    tables: list[str],
    platform: str,
    get_structure: Callable[[str, str], dict[str, Any]],
    execute: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    schema = (schema or "").strip()
    tables = [t.strip() for t in tables if t and str(t).strip()]
    if not schema:
        raise DataQualityError("Schema / layer is required.")
    if not tables:
        raise DataQualityError("Select at least one table.")
    if len(tables) > 12:
        raise DataQualityError("Select at most 12 tables per run.")

    platform = (platform or "postgres").lower()
    results: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []

    for table in tables:
        try:
            structure = get_structure(schema, table)
            columns = list(structure.get("columns") or [])
            pk_cols = [
                str(c["name"])
                for c in columns
                if c.get("primary_key") or c.get("name") in (structure.get("primary_key") or [])
            ]
            if not pk_cols and structure.get("primary_key"):
                pk_cols = [str(x) for x in structure["primary_key"]]

            if platform == "snowflake":
                profile = _profile_table_snowflake(schema, table, columns, pk_cols, execute)
            else:
                # Postgres schemas may be returned as short names
                short = schema.split(".")[-1] if "." in schema else schema
                profile = _profile_table_postgres(short, table, columns, pk_cols, execute)

            scored = _build_checks_and_score(profile, table)
            results.append(
                {
                    "table": table,
                    "schema": schema,
                    "ok": True,
                    **scored,
                }
            )
            all_issues.extend(scored["issues"])
        except Exception as exc:  # noqa: BLE001
            log.exception("DQ failed for %s.%s", schema, table)
            results.append(
                {
                    "table": table,
                    "schema": schema,
                    "ok": False,
                    "score": 0,
                    "grade": "F",
                    "dimensions": {k: 0 for k in WEIGHTS},
                    "checks": [],
                    "issues": [
                        {
                            "severity": "critical",
                            "category": "error",
                            "table": table,
                            "title": "Profiling failed",
                            "detail": str(exc),
                        }
                    ],
                    "error": str(exc),
                }
            )
            all_issues.append(
                {
                    "severity": "critical",
                    "category": "error",
                    "table": table,
                    "title": "Profiling failed",
                    "detail": str(exc),
                }
            )

    ok_scores = [r["score"] for r in results if r.get("ok")]
    overall = round(sum(ok_scores) / len(ok_scores), 1) if ok_scores else 0.0

    # Aggregate dimension averages
    dim_acc = {k: [] for k in WEIGHTS}
    for r in results:
        if not r.get("ok"):
            continue
        for k, v in (r.get("dimensions") or {}).items():
            if k in dim_acc:
                dim_acc[k].append(float(v))
    dim_avg = {
        k: round(sum(v) / len(v), 1) if v else 0.0 for k, v in dim_acc.items()
    }

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    all_issues.sort(key=lambda i: (sev_order.get(i.get("severity", "info"), 9), i.get("table", "")))

    issue_summary = {
        "critical": sum(1 for i in all_issues if i.get("severity") == "critical"),
        "high": sum(1 for i in all_issues if i.get("severity") == "high"),
        "medium": sum(1 for i in all_issues if i.get("severity") == "medium"),
        "low": sum(1 for i in all_issues if i.get("severity") == "low"),
        "total": len(all_issues),
    }

    return {
        "ok": True,
        "connector_id": connector_id,
        "schema": schema,
        "platform": platform,
        "tables": tables,
        "score": overall,
        "grade": _grade(overall),
        "dimensions": dim_avg,
        "issue_summary": issue_summary,
        "issues": all_issues,
        "tables_results": results,
        "logic": score_logic_docs(),
    }
