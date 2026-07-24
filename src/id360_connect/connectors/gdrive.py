"""Google Drive connector — Strategy 4, changes.list with a page token.

AUTH
----
A service account with domain-wide delegation gives tenant-wide reach - powerful
and correspondingly dangerous. Prefer, in order:

  1. A service account added as a member of specific SHARED DRIVES
  2. Per-user OAuth with `drive.readonly`
  3. Domain-wide delegation (last resort, and only if the contract requires it)

Always request the narrowest scope, and always pass `fields=` on every call:
the default response is much larger than what we need.

QUOTAS
------
Per-user and per-project QPS. Back off with jitter on `403 rateLimitExceeded`
and `429`. Note that 403 with a rate-limit reason is a THROTTLE, not an
authorization failure - treating it as fatal is a common bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from ..core.errors import ResyncRequired, SourceThrottled, SourceUnavailable
from ..core.logging import get_logger, log
from ..core.models import ExtractionTask, Op, Position
from ..core.schema import Field, Schema
from .base import Connector, ReadResult

logger = get_logger(__name__)

CHANGE_FIELDS = ("nextPageToken,newStartPageToken,"
                 "changes(fileId,removed,time,file("
                 "id,name,mimeType,size,md5Checksum,parents,createdTime,"
                 "modifiedTime,owners(emailAddress),webViewLink,trashed,driveId))")

#: Native Google formats have no bytes - they must be exported to a concrete
#: MIME type, and export has its own (lower) size limits.
EXPORT_MAP = {
    "application/vnd.google-apps.document":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.drawing": "application/pdf",
}


@dataclass
class DriveOptions:
    drive_id: str | None = None           # None = "My Drive"; set for shared drives
    page_size: int = 1000
    fetch_content: bool = False
    max_content_bytes: int = 100 * 1024 * 1024
    capture_permissions: bool = True
    allowed_mime_types: Sequence[str] = ()
    impersonate_subject: str | None = None    # domain-wide delegation only
    scopes: Sequence[str] = field(
        default_factory=lambda: ("https://www.googleapis.com/auth/drive.readonly",))


class GoogleDriveConnector(Connector):
    kind = "google_drive"

    def __init__(self, source, credential, *, options: DriveOptions | None = None,
                 service=None, **kw):
        super().__init__(source, credential, **kw)
        self.options = options or DriveOptions(**dict(source.options))
        self._service = service           # googleapiclient discovery resource

    # ------------------------------------------------------------------ auth --
    def connect(self) -> None:
        """
        creds = service_account.Credentials.from_service_account_info(
            json.loads(self.credential.get("service_account_json")),
            scopes=self.options.scopes)
        if self.options.impersonate_subject:
            creds = creds.with_subject(self.options.impersonate_subject)
        self._service = build("drive", "v3", credentials=creds,
                              cache_discovery=False)
        """
        raise NotImplementedError("wire to google-api-python-client")

    # -------------------------------------------------------------- metadata --
    def discover(self) -> Sequence[str]:
        return sorted(self.source.objects)

    def fetch_schema(self, object_name: str) -> Schema:
        return Schema(fields=(
            Field("id", "string", False),
            Field("name", "string"),
            Field("mime_type", "string"),
            Field("size", "int64"),
            Field("md5_checksum", "string"),
            Field("parents", "string"),
            Field("owner", "string"),
            Field("created_at", "timestamp"),
            Field("modified_at", "timestamp"),
            Field("web_view_link", "string"),
            Field("permissions", "string"),
            Field("text_content", "string"),
        ))

    def current_position(self, object_name: str) -> Position | None:
        token = self._execute(self._service.changes().getStartPageToken(
            driveId=self.options.drive_id,
            supportsAllDrives=bool(self.options.drive_id)))
        return Position(kind="page_token", value=token["startPageToken"])

    # ------------------------------------------------------------------ read --
    def read(self, task: ExtractionTask) -> Iterator[ReadResult]:
        page_token = (task.since.value if task.since and task.since.kind == "page_token"
                      else self.current_position(task.object_name).value)

        latest: dict[str, dict] = {}
        removed: list[str] = []
        new_start_token: str | None = None

        while page_token:
            self.governor.before_api_call()
            payload = self._execute(self._service.changes().list(
                pageToken=page_token, pageSize=self.options.page_size,
                fields=CHANGE_FIELDS,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                driveId=self.options.drive_id,
                corpora="drive" if self.options.drive_id else "user",
                includeRemoved=True))

            for change in payload.get("changes", []):
                file = change.get("file") or {}
                if change.get("removed") or file.get("trashed"):
                    removed.append(change["fileId"])
                    latest.pop(change["fileId"], None)
                elif self._passes_filter(file):
                    latest[change["fileId"]] = file      # dedupe to latest

            page_token = payload.get("nextPageToken")
            new_start_token = payload.get("newStartPageToken") or new_start_token

        log(logger, 20, "drive changes enumerated",
            object_name=task.object_name, row_count=len(latest) + len(removed))

        if removed:
            yield ReadResult(records=[{"id": fid} for fid in removed], op=Op.DELETE)

        batch: list[dict] = []
        for file in latest.values():
            batch.append(self._to_record(file))
            if len(batch) >= 500:
                self.governor.record_rows(len(batch))
                yield ReadResult(records=batch, op=Op.UPDATE)
                batch = []
        if batch:
            self.governor.record_rows(len(batch))
            yield ReadResult(records=batch, op=Op.UPDATE)

        yield ReadResult(records=[], op=Op.UPDATE, is_last=True,
                         position=Position(kind="page_token", value=new_start_token))

    # -------------------------------------------------------------- helpers --
    def _execute(self, request) -> Mapping[str, Any]:
        """Map Google's error taxonomy onto the framework's.

        The trap: `403 rateLimitExceeded` is a THROTTLE, not an authorization
        failure. Treating it as fatal aborts runs that should simply have
        waited.
        """
        try:
            return request.execute()
        except Exception as exc:                        # noqa: BLE001
            status = getattr(getattr(exc, "resp", None), "status", None)
            reason = str(exc)
            if status == 429 or "rateLimitExceeded" in reason or "userRateLimit" in reason:
                raise SourceThrottled("drive rate limited", retry_after=10.0) from exc
            if status == 404 and "pageToken" in reason:
                raise ResyncRequired("page token invalid; full re-enumeration "
                                     "required") from exc
            if status and status >= 500:
                raise SourceUnavailable("drive 5xx", http_status=status) from exc
            raise SourceUnavailable("drive request failed",
                                    detail=reason[:500]) from exc

    def _passes_filter(self, file: Mapping[str, Any]) -> bool:
        if not file:
            return False
        mime = file.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            return False
        if self.options.allowed_mime_types and mime not in self.options.allowed_mime_types:
            return False
        if (self.options.fetch_content
                and int(file.get("size") or 0) > self.options.max_content_bytes):
            return False
        return True

    def _to_record(self, file: Mapping[str, Any]) -> dict[str, Any]:
        record = {
            "id": file["id"],
            "name": file.get("name"),
            "mime_type": file.get("mimeType"),
            "size": int(file.get("size") or 0),
            "md5_checksum": file.get("md5Checksum"),
            "parents": ";".join(file.get("parents", [])),
            "owner": (file.get("owners") or [{}])[0].get("emailAddress"),
            "created_at": file.get("createdTime"),
            "modified_at": file.get("modifiedTime"),
            "web_view_link": file.get("webViewLink"),
        }
        if self.options.capture_permissions:
            record["permissions"] = self._fetch_permissions(file["id"])
        if self.options.fetch_content:
            record["text_content"] = self._fetch_content(file)
        return record

    def _fetch_permissions(self, file_id: str) -> str:
        import json
        payload = self._execute(self._service.permissions().list(
            fileId=file_id, supportsAllDrives=True,
            fields="permissions(id,type,role,emailAddress,domain)"))
        return json.dumps(payload.get("permissions", []), default=str)

    def _fetch_content(self, file: Mapping[str, Any]) -> str | None:
        """`files.get(alt=media)` for binary files; `files.export` for native
        Google formats.

        Export failures (size limits on large Docs/Sheets) must be handled
        explicitly and surfaced, not silently swallowed - a dropped document is
        a completeness bug that reconciliation will flag later at much higher
        cost.
        """
        mime = file.get("mimeType", "")
        if mime in EXPORT_MAP:
            raise NotImplementedError("files.export with EXPORT_MAP[mime]")
        raise NotImplementedError("files.get(alt=media) with chunked download")
