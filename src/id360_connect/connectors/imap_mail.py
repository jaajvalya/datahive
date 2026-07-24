"""IMAP mailbox connector — for sources without a Graph or Gmail API.

CHANGE DETECTION
----------------
  * `UIDVALIDITY` - if this changes, the folder was recreated and ALL UIDs are
    invalid. Re-sync that folder. Silently continuing produces garbage.
  * `UIDNEXT`     - new messages have UID >= the last seen UID.
  * `MODSEQ` (CONDSTORE/QRESYNC) - catches FLAG changes and deletions, which a
    UID scan alone cannot see. Use it whenever the server advertises it.

PERFORMANCE
-----------
One FETCH per message is pathological on a large folder. Use UID ranges and
request only the parts needed. And always `BODY.PEEK[...]`, never `BODY[...]` -
the latter sets the \\Seen flag and marks the user's mail as read, which is an
embarrassing and very visible bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from ..core.errors import ResyncRequired, SourceUnavailable
from ..core.logging import get_logger, log
from ..core.models import ExtractionTask, Op, Position
from ..core.schema import Field, Schema
from .base import Connector, ReadResult

logger = get_logger(__name__)

HEADER_PARTS = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)]"


@dataclass
class ImapOptions:
    port: int = 993
    use_ssl: bool = True                  # never plaintext IMAP
    batch_size: int = 500
    fetch_bodies: bool = False            # headers-only by default
    fetch_attachments: bool = False
    max_message_bytes: int = 25 * 1024 * 1024
    use_condstore: bool = True


class ImapConnector(Connector):
    kind = "imap"

    def __init__(self, source, credential, *, options: ImapOptions | None = None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or ImapOptions(**dict(source.options))
        self._client = None

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> None:
        from imapclient import IMAPClient
        if not self.options.use_ssl:
            from ..core.errors import InsecureTransport
            raise InsecureTransport("plaintext IMAP is refused; enable TLS or "
                                    "record a documented exception")
        try:
            self._client = IMAPClient(self.source.endpoint, port=self.options.port,
                                      ssl=True, use_uid=True)
            self._client.login(self.credential.get("username"),
                               self.credential.get("password"))
        except Exception as exc:                        # noqa: BLE001
            raise SourceUnavailable("imap connect failed", detail=str(exc)) from exc

    def close(self) -> None:
        if self._client:
            try:
                self._client.logout()
            finally:
                self._client = None

    # -------------------------------------------------------------- metadata --
    def discover(self) -> Sequence[str]:
        allowed = set(self.source.objects)
        return sorted(name for _flags, _delim, name in self._client.list_folders()
                      if name in allowed)

    def fetch_schema(self, object_name: str) -> Schema:
        return Schema(fields=(
            Field("uid", "int64", False),
            Field("message_id", "string"),
            Field("subject", "string"),
            Field("from_address", "string"),
            Field("to_addresses", "string"),
            Field("cc_addresses", "string"),
            Field("date", "timestamp"),
            Field("size", "int64"),
            Field("flags", "string"),
            Field("body_text", "string"),
            Field("attachment_hashes", "string"),
        ))

    def current_position(self, object_name: str) -> Position | None:
        info = self._client.select_folder(object_name, readonly=True)
        return Position(kind="imap_uid", value={
            "uidvalidity": int(info[b"UIDVALIDITY"]),
            "uid": int(info[b"UIDNEXT"]) - 1,
            "modseq": int(info.get(b"HIGHESTMODSEQ", 0)),
        })

    # ------------------------------------------------------------------ read --
    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        folder = task.object_name
        info = self._client.select_folder(folder, readonly=True)
        uidvalidity = int(info[b"UIDVALIDITY"])
        highest_modseq = int(info.get(b"HIGHESTMODSEQ", 0))

        since = task.since.value if task.since else {}
        last_uid = int(since.get("uid", 0))
        last_modseq = int(since.get("modseq", 0))

        # The check that saves you from silent corruption.
        if since and int(since.get("uidvalidity", uidvalidity)) != uidvalidity:
            raise ResyncRequired(
                "UIDVALIDITY changed; the folder was recreated and all stored "
                "UIDs are invalid",
                object_name=folder)

        if self.options.use_condstore and last_modseq and highest_modseq:
            # Catches flag changes and deletions that a plain UID scan misses.
            uids = self._client.search(["MODSEQ", str(last_modseq + 1)])
        else:
            uids = self._client.search([f"UID {last_uid + 1}:*"])

        uids = [u for u in uids if u > last_uid or self.options.use_condstore]
        log(logger, 20, "imap messages selected",
            object_name=folder, row_count=len(uids))

        parts = ["ENVELOPE", "RFC822.SIZE", "FLAGS", HEADER_PARTS]
        if self.options.fetch_bodies:
            parts.append("BODY.PEEK[TEXT]")   # PEEK: never set \\Seen

        max_uid = last_uid
        for chunk in self._chunks(uids, self.options.batch_size):
            self.governor.before_api_call()
            fetched = self._client.fetch(chunk, parts)
            records = []
            for uid, data in fetched.items():
                records.append(self._to_record(uid, data))
                max_uid = max(max_uid, int(uid))
            self.governor.record_rows(len(records))
            yield ReadResult(records=records, op=Op.UPDATE)

        yield ReadResult(records=[], op=Op.UPDATE, is_last=True,
                         position=Position(kind="imap_uid", value={
                             "uidvalidity": uidvalidity,
                             "uid": max_uid,
                             "modseq": highest_modseq}))

    # -------------------------------------------------------------- helpers --
    def _to_record(self, uid: int, data: dict) -> dict[str, Any]:
        envelope = data.get(b"ENVELOPE")
        decode = lambda v: v.decode(errors="replace") if isinstance(v, bytes) else v  # noqa: E731

        def addresses(field) -> str:
            if not field:
                return ""
            return ";".join(
                f"{decode(a.mailbox)}@{decode(a.host)}"
                for a in field if a.mailbox and a.host)

        record = {
            "uid": int(uid),
            "message_id": decode(getattr(envelope, "message_id", None)),
            "subject": decode(getattr(envelope, "subject", None)),
            "from_address": addresses(getattr(envelope, "from_", None)),
            "to_addresses": addresses(getattr(envelope, "to", None)),
            "cc_addresses": addresses(getattr(envelope, "cc", None)),
            "date": getattr(envelope, "date", None),
            "size": data.get(b"RFC822.SIZE"),
            "flags": ";".join(decode(f) for f in data.get(b"FLAGS", ())),
        }
        if self.options.fetch_bodies:
            record["body_text"] = self._parse_body(data)
        return record

    @staticmethod
    def _parse_body(data: dict) -> str | None:
        """Use `email.parser`, never regex.

        Handle nested multipart, TNEF (winmail.dat), inline vs attached parts,
        and charset mislabelling - real-world mail violates the spec constantly
        and a naive parser drops content silently.
        """
        raise NotImplementedError("MIME-parse with email.parser + charset repair")

    @staticmethod
    def _chunks(items, size: int):
        for i in range(0, len(items), size):
            yield items[i:i + size]
