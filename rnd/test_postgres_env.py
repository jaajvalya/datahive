"""Verify PostgreSQL settings from repo-root `.env` (does not print passwords)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import postgres_store as ps  # noqa: E402


def main() -> int:
    kw = ps.postgres_dsn_kwargs()
    print("Target:", f"{kw['user']}@{kw['host']}:{kw['port']}/{kw['dbname']}")
    print("Password configured:", bool(kw.get("password")))
    if ps.postgres_conninfo():
        print("Using POSTGRES_CONNINFO / POSTGRES_URI from .env")
    try:
        ps.ping_postgres()
    except Exception as exc:
        print("Connection failed:", str(exc).splitlines()[0])
        print()
        print("Fix: set POSTGRES_USER / POSTGRES_PASSWORD in .env to match your")
        print("PostgreSQL server, or set POSTGRES_CONNINFO. See .env.example.")
        return 1
    schemas = ps.list_schemas()
    print("Connected OK. Asset schemas:", schemas)
    for schema in schemas:
        tables = ps.list_tables(schema)
        print(f"  {schema}: {len(tables)} objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
