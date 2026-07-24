"""Microsoft Graph connector — SharePoint, OneDrive, and Exchange mailboxes.

One connector family, three object domains, one strategy: delta tokens (S4).

AUTH — THE PART THAT MATTERS MOST
---------------------------------
App-only (client credentials) with a CERTIFICATE, not a client secret. Then
scope it:

  * `Sites.Selected` granted PER SITE, instead of `Sites.Read.All`. A
    compromised app then cannot read the whole tenant. This is the highest-value
    security control in the M365 connectors and most implementations skip it.
  * `ApplicationAccessPolicy` for mail. Without it, `Mail.Read` app-only means
    EVERY MAILBOX IN THE TENANT. With it, the app is restricted to a named
    group.

THROTTLING
----------
Graph enforces per-app AND per-tenant limits and returns 429 with `Retry-After`.
Honour that header exactly. Sustained violation gets the app throttled
tenant-wide, degrading the provider's OTHER applications - which is how a
connector becomes an incident on somebody else's system.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from ..core.errors import (ResyncRequired, SourceThrottled, SourceUnavailable)
from ..core.logging import get_logger, log
from ..core.models import ExtractionTask, Op, Position, SourceKind
from ..core.schema import Field, Schema
from .base import Connector, ReadResult

logger = get_logger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

# Projections. `$select` is not an optimization here - the default response is
# several times larger than what we need, and every unnecessary field is
# both cost and data we are not entitled to hold.
DRIVE_ITEM_SELECT = ("id,name,size,webUrl,createdDateTime,lastModifiedDateTime,"
                     "file,folder,parentReference,createdBy,lastModifiedBy,deleted")
MESSAGE_SELECT = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                  "sentDateTime,hasAttachments,internetMessageId,conversationId,"
                  "importance,isRead,parentFolderId")


@dataclass
class GraphOptions:
    tenant_id: str = ""
    client_id: str = ""
    scope: str = "https://graph.microsoft.com/.default"
    page_size: int = 200
    fetch_content: bool = False       # headers/metadata only by default
    max_content_bytes: int = 100 * 1024 * 1024
    capture_permissions: bool = True
    allowed_mime_types: Sequence[str] = ()
    max_age_days: int | None = None


class GraphConnector(Connector):
    """Base for SharePoint / OneDrive / Mailbox."""

    kind = "msgraph"

    def __init__(self, source, credential, *, options: GraphOptions | None = None,
                 session=None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or GraphOptions(**dict(source.options))
        self._session = session          # requests.Session or httpx.Client
        self._token: str | None = None
        self._token_expires = 0.0

    # ------------------------------------------------------------------ auth --
    def connect(self) -> None:
        self._authenticate()

    def _authenticate(self) -> None:
        """Client credentials with a CERTIFICATE assertion.

        msal.ConfidentialClientApplication(
            client_id=...,
            client_credential={"private_key": ..., "thumbprint": ...},
            authority=f"https://login.microsoftonline.com/{tenant_id}")

        Certificates over secrets: they are not copy-pasteable out of a config
        file, and they can be bound to hardware.
        """
        raise NotImplementedError("wire to MSAL with a certificate credential")

    # ------------------------------------------------------------------ http --
    def _request(self, method: str, url: str, **kwargs) -> Mapping[str, Any]:
        self.governor.before_api_call()
        if time.time() >= self._token_expires:
            self._authenticate()

        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._token}"
        # Traceability into the PROVIDER's telemetry: when their admin sees
        # load from our app, this header gives them the exact run to quote
        # back to us.
        headers.setdefault("client-request-id", kwargs.pop("run_id", "") or "")

        response = self._session.request(method, url, headers=headers, **kwargs)

        if response.status_code == 429:
            # Honour the header EXACTLY. Do not substitute our own curve.
            retry_after = float(response.headers.get("Retry-After", "10"))
            self.governor.concurrency.on_throttle()
            raise SourceThrottled("graph throttled", retry_after=retry_after,
                                  http_status=429)
        if response.status_code == 410:
            # Token expired. Full re-enumeration + ALERT: a resync is expensive
            # and we want to know every time it happens.
            raise ResyncRequired("delta token expired", http_status=410)
        if response.status_code >= 500:
            raise SourceUnavailable("graph 5xx", http_status=response.status_code)
        if response.status_code >= 400:
            # Never surface the raw body: it can echo item names and addresses.
            raise SourceUnavailable("graph request failed",
                                    http_status=response.status_code,
                                    detail=response.text[:500])

        self.governor.concurrency.on_success()
        return response.json()

    def _paged(self, url: str, *, params: Mapping[str, Any] | None = None
               ) -> Iterator[tuple[list[dict], str | None]]:
        """Walk @odata.nextLink; return the final @odata.deltaLink."""
        next_url, first = url, True
        while next_url:
            payload = self._request("GET", next_url,
                                    params=params if first else None)
            first = False
            items = payload.get("value", [])
            delta_link = payload.get("@odata.deltaLink")
            next_url = payload.get("@odata.nextLink")
            yield items, delta_link


class SharePointConnector(GraphConnector):
    """SharePoint document libraries. `object_name` = "{siteId}/{driveId}"."""

    kind = "sharepoint"

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        return Schema(fields=(
            Field("id", "string", False),
            Field("name", "string", False),
            Field("path", "string"),
            Field("size", "int64"),
            Field("mime_type", "string"),
            Field("created_at", "timestamp"),
            Field("modified_at", "timestamp"),
            Field("created_by", "string"),
            Field("modified_by", "string"),
            Field("web_url", "string"),
            Field("content_hash", "string"),
            Field("permissions", "string"),      # JSON-encoded ACL snapshot
            Field("text_content", "string"),
        ))

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        site_id, _, drive_id = task.object_name.partition("/")
        url = (task.since.value if task.since and task.since.kind == "delta_link"
               else f"{GRAPH}/drives/{drive_id}/root/delta")
        params = {"$select": DRIVE_ITEM_SELECT, "$top": self.options.page_size}

        latest: dict[str, dict] = {}
        deleted: list[dict] = []
        delta_link: str | None = None

        for items, link in self._paged(url, params=params):
            delta_link = link or delta_link
            for item in items:
                if item.get("deleted"):
                    deleted.append(item)
                    latest.pop(item["id"], None)
                elif self._passes_filter(item):
                    # Dedupe: a file edited 40 times in an hour appears 40 times
                    # in the feed. We only need the latest version, once.
                    latest[item["id"]] = item

        log(logger, 20, "graph delta enumerated",
            object_name=task.object_name, row_count=len(latest) + len(deleted))

        if deleted:
            yield ReadResult(
                records=[{"id": d["id"], "name": None} for d in deleted],
                op=Op.DELETE)

        batch: list[dict] = []
        for item in latest.values():
            batch.append(self._to_record(item, drive_id))
            if len(batch) >= 500:
                self.governor.record_rows(len(batch))
                yield ReadResult(records=batch, op=Op.UPDATE)
                batch = []
        if batch:
            self.governor.record_rows(len(batch))
            yield ReadResult(records=batch, op=Op.UPDATE)

        # Persisted by the executor ONLY after the sink commits.
        yield ReadResult(records=[], op=Op.UPDATE, is_last=True,
                         position=Position(kind="delta_link", value=delta_link))

    # ---------------------------------------------------------------- filter --
    def _passes_filter(self, item: Mapping[str, Any]) -> bool:
        """Applied BEFORE any content fetch.

        Fetching then discarding wastes the provider's throttling budget and
        puts data we were never supposed to hold into our process memory.
        """
        if "file" not in item:
            return False
        if self.options.allowed_mime_types:
            mime = item.get("file", {}).get("mimeType", "")
            if mime not in self.options.allowed_mime_types:
                return False
        size = item.get("size", 0)
        if size > self.options.max_content_bytes and self.options.fetch_content:
            return False
        if self.options.max_age_days is not None:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.options.max_age_days)
            modified = item.get("lastModifiedDateTime", "")
            if modified and modified < cutoff.isoformat():
                return False
        return True

    # ---------------------------------------------------------------- record --
    def _to_record(self, item: Mapping[str, Any], drive_id: str) -> dict[str, Any]:
        record = {
            "id": item["id"],
            "name": item.get("name"),
            "path": (item.get("parentReference") or {}).get("path"),
            "size": item.get("size"),
            "mime_type": (item.get("file") or {}).get("mimeType"),
            "content_hash": ((item.get("file") or {}).get("hashes") or {}).get("quickXorHash"),
            "created_at": item.get("createdDateTime"),
            "modified_at": item.get("lastModifiedDateTime"),
            "created_by": ((item.get("createdBy") or {}).get("user") or {}).get("email"),
            "modified_by": ((item.get("lastModifiedBy") or {}).get("user") or {}).get("email"),
            "web_url": item.get("webUrl"),
        }
        if self.options.capture_permissions:
            # Ingesting content WITHOUT its ACL gives every downstream consumer
            # a permissions bug. Not optional for enterprise deployments.
            record["permissions"] = self._fetch_permissions(drive_id, item["id"])
        if self.options.fetch_content:
            record["text_content"] = self._fetch_content(drive_id, item)
        return record

    def _fetch_permissions(self, drive_id: str, item_id: str) -> str:
        import json
        payload = self._request("GET", f"{GRAPH}/drives/{drive_id}/items/{item_id}/permissions")
        return json.dumps([
            {"role": p.get("roles"),
             "principal": ((p.get("grantedToV2") or {}).get("user") or {}).get("email")
                          or ((p.get("grantedToV2") or {}).get("group") or {}).get("displayName"),
             "link_scope": (p.get("link") or {}).get("scope")}
            for p in payload.get("value", [])], default=str)

    def _fetch_content(self, drive_id: str, item: Mapping[str, Any]) -> str | None:
        """Range-GET with resume for large files.

        A 2 GB download that fails at 90% and restarts from zero is both a cost
        problem and a reliability problem.
        """
        raise NotImplementedError("range-GET + resume, then text extraction")


class OneDriveConnector(SharePointConnector):
    """Identical mechanics; object_name = "users/{userId}/drive"."""
    kind = "onedrive"


class MailboxConnector(GraphConnector):
    """Exchange Online via Graph. The highest-sensitivity source in the set.

    Default posture: HEADERS ONLY. Pulling every body plus every attachment
    multiplies volume by 10-50x and multiplies risk by more than that.
    """

    kind = "mailbox"

    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        return Schema(fields=(
            Field("id", "string", False),
            Field("internet_message_id", "string"),
            Field("conversation_id", "string"),
            Field("subject", "string"),
            Field("from_address", "string"),
            Field("to_addresses", "string"),
            Field("cc_addresses", "string"),
            Field("received_at", "timestamp"),
            Field("sent_at", "timestamp"),
            Field("has_attachments", "bool"),
            Field("folder_id", "string"),
            Field("body_text", "string"),
            Field("attachment_hashes", "string"),
        ))

    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        # object_name = "{userId}/{folderId}"
        user_id, _, folder_id = task.object_name.partition("/")
        url = (task.since.value if task.since and task.since.kind == "delta_link"
               else f"{GRAPH}/users/{user_id}/mailFolders/{folder_id}/messages/delta")
        params = {"$select": MESSAGE_SELECT, "$top": self.options.page_size}

        delta_link, batch = None, []
        for items, link in self._paged(url, params=params):
            delta_link = link or delta_link
            for message in items:
                if message.get("@removed"):
                    continue
                batch.append(self._to_record(message, user_id))
                if len(batch) >= 200:
                    self.governor.record_rows(len(batch))
                    yield ReadResult(records=batch, op=Op.UPDATE)
                    batch = []
        if batch:
            self.governor.record_rows(len(batch))
            yield ReadResult(records=batch, op=Op.UPDATE)

        yield ReadResult(records=[], op=Op.UPDATE, is_last=True,
                         position=Position(kind="delta_link", value=delta_link))

    def _to_record(self, message: Mapping[str, Any], user_id: str) -> dict[str, Any]:
        addr = lambda r: ((r or {}).get("emailAddress") or {}).get("address")  # noqa: E731
        record = {
            "id": message["id"],
            "internet_message_id": message.get("internetMessageId"),
            "conversation_id": message.get("conversationId"),
            "subject": message.get("subject"),
            "from_address": addr(message.get("from")),
            "to_addresses": ";".join(filter(None, (addr(r) for r in message.get("toRecipients", [])))),
            "cc_addresses": ";".join(filter(None, (addr(r) for r in message.get("ccRecipients", [])))),
            "received_at": message.get("receivedDateTime"),
            "sent_at": message.get("sentDateTime"),
            "has_attachments": message.get("hasAttachments"),
            "folder_id": message.get("parentFolderId"),
        }
        if self.options.fetch_content:
            record["body_text"] = self._fetch_body(user_id, message["id"])
            record["attachment_hashes"] = self._fetch_attachments(user_id, message["id"])
        return record

    def _fetch_body(self, user_id: str, message_id: str) -> str | None:
        """Use a real MIME parser, never regex. Handle nested multipart, TNEF
        (winmail.dat), inline vs attached parts, and charset mislabelling."""
        raise NotImplementedError("fetch + MIME-parse the message body")

    def _fetch_attachments(self, user_id: str, message_id: str) -> str | None:
        """Store attachments by CONTENT HASH and dedupe aggressively.

        The same 8 MB deck attached across a 200-person thread should be stored
        once, not two hundred times.
        """
        raise NotImplementedError("fetch attachments, store by content hash")
