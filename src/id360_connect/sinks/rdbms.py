"""RDBMS sink — Postgres (or any DB-API driver).

Idempotency without a table format:

  * A `committed_batches` table with a UNIQUE constraint on batch_id. The
    INSERT is the idempotency token: if it conflicts, the batch already landed
    and the whole apply is skipped.
  * Staging table + MERGE/UPSERT keyed on the business PK, all inside one
    transaction with the token insert. All or nothing.
  * Full refresh uses staging + atomic table swap, so readers never see an
    empty or half-loaded table.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..core.errors import CommitFailed, SinkUnavailable
from ..core.models import BatchMeta
from .base import Sink, WriteResult

DDL_COMMITTED_BATCHES = """
CREATE TABLE IF NOT EXISTS id360_committed_batches (
    batch_id      TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    object_name   TEXT NOT NULL,
    row_count     BIGINT NOT NULL,
    byte_count    BIGINT NOT NULL,
    committed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class RdbmsConfig:
    schema: str = "id360_staging"
    target_schema: str = "id360"
    mode: str = "upsert"                 # "upsert" | "append" | "full_refresh"
    primary_key: Sequence[str] = ()
    batch_rows: int = 10_000


class RdbmsSink(Sink):
    name = "rdbms"

    def __init__(self, connection, config: RdbmsConfig):
        self.conn = connection           # DB-API connection, autocommit OFF
        self.config = config

    # ---------------------------------------------------------------- open --
    def open(self, meta: BatchMeta) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.config.schema}"')
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.config.target_schema}"')
            cur.execute(DDL_COMMITTED_BATCHES)
        self.conn.commit()

    # --------------------------------------------------------------- write --
    def write_batch(self, records: Iterable[Mapping[str, Any]],
                    meta: BatchMeta) -> WriteResult:
        rows = list(records)
        if not rows:
            return WriteResult(uri=self._target(meta), byte_count=0, row_count=0,
                               content_sha256=self.checksum(b""))

        columns = list(rows[0].keys())
        staging = self._staging(meta)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        for row in rows:
            writer.writerow(row)
        payload = buf.getvalue().encode()

        try:
            with self.conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {staging}')
                cur.execute(
                    f'CREATE UNLOGGED TABLE {staging} '
                    f'(LIKE {self._target(meta)} INCLUDING DEFAULTS)')
                # Postgres COPY is an order of magnitude faster than executemany.
                cur.copy_expert(
                    f'COPY {staging} ({",".join(self._quote(c) for c in columns)}) '
                    f'FROM STDIN WITH (FORMAT csv)',
                    io.StringIO(payload.decode()))
        except Exception as exc:                        # noqa: BLE001
            self.conn.rollback()
            raise SinkUnavailable("staging load failed", detail=str(exc)) from exc

        return WriteResult(uri=self._target(meta), byte_count=len(payload),
                           row_count=len(rows), content_sha256=self.checksum(payload))

    # -------------------------------------------------------------- commit --
    def commit(self, meta: BatchMeta) -> None:
        """One transaction: idempotency token + apply. All or nothing."""
        staging, target = self._staging(meta), self._target(meta)
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO id360_committed_batches "
                    "(batch_id, run_id, tenant_id, object_name, row_count, byte_count) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (batch_id) DO NOTHING",
                    (meta.batch_id, meta.run_id, meta.tenant_id, meta.object_name,
                     meta.row_count, meta.byte_count))
                if cur.rowcount == 0:
                    self.conn.rollback()        # already committed - no-op
                    return

                if self.config.mode == "full_refresh":
                    cur.execute(f'ALTER TABLE {target} RENAME TO '
                                f'{self._ident(meta)}_old')
                    cur.execute(f'ALTER TABLE {staging} RENAME TO {self._ident(meta)}')
                    cur.execute(f'DROP TABLE IF EXISTS {self._ident(meta)}_old')
                elif self.config.mode == "append":
                    cur.execute(f'INSERT INTO {target} SELECT * FROM {staging}')
                else:
                    cur.execute(self._merge_sql(meta, staging, target))

                cur.execute(f'DROP TABLE IF EXISTS {staging}')
            self.conn.commit()
        except Exception as exc:                        # noqa: BLE001
            self.conn.rollback()
            raise CommitFailed("rdbms commit failed", detail=str(exc)) from exc

    def abort(self, meta: BatchMeta) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {self._staging(meta)}')
            self.conn.commit()
        except Exception:                               # noqa: BLE001
            self.conn.rollback()

    # -------------------------------------------------------------- helpers --
    def _merge_sql(self, meta: BatchMeta, staging: str, target: str) -> str:
        pk = list(self.config.primary_key) or ["_id360_row_hash"]
        on = " AND ".join(f"t.{self._quote(c)} = s.{self._quote(c)}" for c in pk)
        return f"""
            MERGE INTO {target} AS t
            USING {staging} AS s ON {on}
            WHEN MATCHED AND s._id360_op = 'D' THEN DELETE
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED AND s._id360_op <> 'D' THEN INSERT *
        """

    def _ident(self, meta: BatchMeta) -> str:
        return self._quote(meta.object_name.replace(".", "_"))

    def _staging(self, meta: BatchMeta) -> str:
        return (f'{self._quote(self.config.schema)}.'
                f'{self._quote(meta.object_name.replace(".", "_"))}_'
                f'{meta.batch_id[-8:]}')

    def _target(self, meta: BatchMeta) -> str:
        return f'{self._quote(self.config.target_schema)}.{self._ident(meta)}'

    @staticmethod
    def _quote(identifier: str) -> str:
        """Identifier quoting. Object names come from the registry allowlist,
        never from user input - this is belt-and-braces against injection."""
        return '"' + identifier.replace('"', '""') + '"'
