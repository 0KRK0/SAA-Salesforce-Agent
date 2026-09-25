"""Salesforce API client: REST, SOQL, Composite, Tooling, Bulk v2.

The client owns the token lifecycle (resolve reference -> use -> refresh ->
store new reference) so that no caller — and certainly not the model — ever
handles a credential. The plaintext token exists only on this object, for the
lifetime of the request, and is never returned, logged or serialized.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SalesforceConnection
from app.observability.logging import get_logger
from app.salesforce.errors import SalesforceAuthError, SalesforceError, from_response
from app.security.secrets import SecretContext, fingerprint, resolve_secret, store_secret

log = get_logger("salesforce.client")

_RETRY_STATUS = {500, 502, 503, 504}


@dataclass
class ApiUsage:
    used: int | None = None
    limit: int | None = None

    def parse(self, header: str | None) -> None:
        # Sforce-Limit-Info: api-usage=123/15000
        if not header:
            return
        try:
            part = header.split("api-usage=")[1].split(",")[0]
            used, limit = part.split("/")
            self.used, self.limit = int(used), int(limit)
        except (IndexError, ValueError):  # pragma: no cover - header shape changed
            self.used = self.limit = None


class SalesforceClient:
    """Per-connection Salesforce client.

    Usage:
        async with SalesforceClient(conn, session) as sf:
            await sf.describe("Account")
    """

    def __init__(
        self,
        connection: SalesforceConnection,
        db: AsyncSession | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.conn = connection
        self.db = db
        self._http = http
        self._owns_http = http is None
        self._secret_ctx = SecretContext(
            company_id=connection.company_id,
            project_id=connection.project_id,
            purpose="salesforce_token",
        )
        self._access_token = resolve_secret(
            connection.access_token_ref, self._secret_ctx
        )
        self.api_version = connection.api_version or settings.salesforce_api_version
        self.usage = ApiUsage()
        self._describe_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ infra
    async def __aenter__(self) -> SalesforceClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def access_token(self) -> str:
        """Server-side only. Never serialize this into a tool result or prompt."""
        return self._access_token

    @property
    def instance_url(self) -> str:
        return self.conn.instance_url.rstrip("/")

    @property
    def base(self) -> str:
        return f"{self.instance_url}/services/data/v{self.api_version}"

    @property
    def tooling_base(self) -> str:
        return f"{self.base}/tooling"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    async def _refresh(self) -> None:
        from app.salesforce.apps import client_for_app_id
        from app.salesforce.oauth import refresh_access_token

        if not self.conn.refresh_token_ref:
            raise SalesforceAuthError(
                error_type="NO_REFRESH_TOKEN",
                message="Salesforce session expired and no refresh token is stored.",
                likely_cause=(
                    "The connected app did not grant refresh_token / offline_access."
                ),
                suggested_action="Reconnect the Salesforce org from the Connections page.",
            )
        if self.db is None:
            raise SalesforceAuthError(
                error_type="NO_SESSION",
                message="Cannot refresh a Salesforce token without a database session.",
                likely_cause=(
                    "The Salesforce app that issued this token has to be looked up "
                    "before the refresh can be presented to it."
                ),
                suggested_action="Reconnect the Salesforce org from the Connections page.",
            )
        oauth_client = await client_for_app_id(
            self.db, self.conn.salesforce_app_id, company_id=self.conn.company_id
        )
        bundle = await refresh_access_token(
            self.conn.login_url or settings.salesforce_login_url,
            resolve_secret(self.conn.refresh_token_ref, self._secret_ctx),
            oauth_client,
        )
        self._access_token = bundle.access_token
        self.conn.access_token_ref = store_secret(bundle.access_token, self._secret_ctx)
        if bundle.refresh_token:
            self.conn.refresh_token_ref = store_secret(
                bundle.refresh_token, self._secret_ctx
            )
        self.conn.instance_url = bundle.instance_url.rstrip("/")
        self.conn.token_issued_at = bundle.issued_at
        self.conn.token_fingerprint = fingerprint(bundle.access_token)
        if self.db is not None:
            await self.db.flush()
        log.info(
            "salesforce.token_refreshed", salesforce_connection_id=self.conn.id
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | str | None = None,
        allow_refresh: bool = True,
        max_attempts: int = 3,
        expect_json: bool = True,
    ) -> Any:
        if self._http is None:  # pragma: no cover - convenience path
            self._http = httpx.AsyncClient(timeout=60.0)
            self._owns_http = True

        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            t0 = time.perf_counter()
            resp = await self._http.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers(headers),
                content=content,
            )
            elapsed = (time.perf_counter() - t0) * 1000
            self.usage.parse(resp.headers.get("Sforce-Limit-Info"))
            log.debug(
                "salesforce.http",
                method=method,
                url=url.split("?")[0],
                status=resp.status_code,
                ms=round(elapsed, 1),
                api_used=self.usage.used,
                api_limit=self.usage.limit,
            )

            if resp.status_code < 300:
                if not expect_json or not resp.content:
                    return resp
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.json()
                return resp

            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            err = from_response(resp.status_code, payload)

            if (
                resp.status_code == 401
                and allow_refresh
                and not refreshed
                and self.conn.refresh_token_ref
            ):
                refreshed = True
                await self._refresh()
                continue

            if resp.status_code in _RETRY_STATUS and attempt < max_attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            if err.error_type == "UNABLE_TO_LOCK_ROW" and attempt < max_attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            raise err

    # ------------------------------------------------------------- identity
    async def validate(self) -> dict[str, Any]:
        """Cheap connectivity check; also refreshes org metadata."""
        data = await self.request("GET", f"{self.base}/limits")
        org = await self.query(
            "SELECT Id, Name, OrganizationType, IsSandbox, InstanceName "
            "FROM Organization LIMIT 1"
        )
        record = (org.get("records") or [{}])[0]
        self.conn.last_validated_at = datetime.now(UTC)
        self.conn.is_sandbox = bool(record.get("IsSandbox", self.conn.is_sandbox))
        self.conn.org_type = str(record.get("OrganizationType") or self.conn.org_type)
        return {
            "org_id": record.get("Id"),
            "org_name": record.get("Name"),
            "org_type": record.get("OrganizationType"),
            "is_sandbox": record.get("IsSandbox"),
            "instance": record.get("InstanceName"),
            "api_version": self.api_version,
            "daily_api_requests": data.get("DailyApiRequests"),
        }

    # -------------------------------------------------------------- describe
    async def describe_global(self) -> dict[str, Any]:
        return await self.request("GET", f"{self.base}/sobjects")

    async def describe(self, sobject: str, use_cache: bool = True) -> dict[str, Any]:
        key = sobject.lower()
        if use_cache and key in self._describe_cache:
            return self._describe_cache[key]
        data = await self.request("GET", f"{self.base}/sobjects/{sobject}/describe")
        self._describe_cache[key] = data
        return data

    def invalidate_describe(self, sobject: str) -> None:
        self._describe_cache.pop(sobject.lower(), None)

    # ----------------------------------------------------------------- query
    async def query(self, soql: str, tooling: bool = False) -> dict[str, Any]:
        base = self.tooling_base if tooling else self.base
        return await self.request("GET", f"{base}/query", params={"q": soql})

    async def query_all_pages(
        self, soql: str, max_records: int, tooling: bool = False
    ) -> dict[str, Any]:
        data = await self.query(soql, tooling=tooling)
        records = list(data.get("records") or [])
        next_url = data.get("nextRecordsUrl")
        while next_url and len(records) < max_records:
            data = await self.request("GET", f"{self.instance_url}{next_url}")
            records.extend(data.get("records") or [])
            next_url = data.get("nextRecordsUrl")
        truncated = len(records) > max_records
        return {
            "totalSize": data.get("totalSize", len(records)),
            "done": data.get("done", True) and not next_url,
            "records": records[:max_records],
            "truncated": truncated,
            "nextRecordsUrl": next_url,
        }

    # ---------------------------------------------------------------- records
    async def create_record(self, sobject: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"{self.base}/sobjects/{sobject}", json=data)

    async def update_record(
        self, sobject: str, record_id: str, data: dict[str, Any]
    ) -> None:
        await self.request(
            "PATCH",
            f"{self.base}/sobjects/{sobject}/{record_id}",
            json=data,
            expect_json=False,
        )

    async def delete_record(self, sobject: str, record_id: str) -> None:
        await self.request(
            "DELETE", f"{self.base}/sobjects/{sobject}/{record_id}", expect_json=False
        )

    async def get_record(
        self, sobject: str, record_id: str, fields: list[str] | None = None
    ) -> dict[str, Any]:
        params = {"fields": ",".join(fields)} if fields else None
        return await self.request(
            "GET", f"{self.base}/sobjects/{sobject}/{record_id}", params=params
        )

    async def composite(self, requests: list[dict[str, Any]], all_or_none: bool = True):
        return await self.request(
            "POST",
            f"{self.base}/composite",
            json={"allOrNone": all_or_none, "compositeRequest": requests},
        )

    # ---------------------------------------------------------------- tooling
    async def tooling_query(self, soql: str) -> dict[str, Any]:
        return await self.query(soql, tooling=True)

    async def tooling_create(self, sobject: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "POST", f"{self.tooling_base}/sobjects/{sobject}", json=data
        )

    async def run_anonymous_apex(self, apex: str) -> dict[str, Any]:
        return await self.request(
            "GET", f"{self.tooling_base}/executeAnonymous", params={"anonymousBody": apex}
        )

    # ------------------------------------------------------------- bulk v2
    async def bulk_query(
        self, soql: str, max_records: int = 50_000, poll_interval: float = 2.0,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Bulk API 2.0 query job — the correct path for large extracts.

        Returns parsed rows (capped at max_records) plus the job id so callers
        can reference the real Salesforce job.
        """
        job = await self.request(
            "POST",
            f"{self.base}/jobs/query",
            json={"operation": "query", "query": soql},
        )
        job_id = job["id"]
        deadline = time.monotonic() + timeout
        state = job.get("state", "UploadComplete")
        while state not in {"JobComplete", "Failed", "Aborted"}:
            if time.monotonic() > deadline:
                raise SalesforceError(
                    error_type="BULK_QUERY_TIMEOUT",
                    message=f"Bulk query job {job_id} did not finish within {timeout}s.",
                    likely_cause="The extract is very large or the org is busy.",
                    suggested_action="Narrow the query or poll the job id later.",
                    retryable=True,
                )
            await asyncio.sleep(poll_interval)
            job = await self.request("GET", f"{self.base}/jobs/query/{job_id}")
            state = job.get("state", "")

        if state != "JobComplete":
            raise SalesforceError(
                error_type="BULK_QUERY_FAILED",
                message=job.get("errorMessage", f"Bulk job ended in state {state}."),
                likely_cause="Salesforce rejected the bulk query.",
                suggested_action="Validate the SOQL with a small LIMIT query first.",
            )

        resp = await self.request(
            "GET",
            f"{self.base}/jobs/query/{job_id}/results",
            params={"maxRecords": min(max_records, 100_000)},
            headers={"Accept": "text/csv"},
            expect_json=False,
        )
        text = resp.text if hasattr(resp, "text") else str(resp)
        rows = list(csv.DictReader(io.StringIO(text)))
        return {
            "job_id": job_id,
            "record_count": len(rows[:max_records]),
            "records": rows[:max_records],
            "truncated": len(rows) > max_records,
        }

    async def bulk_ingest(
        self,
        sobject: str,
        operation: str,
        rows: list[dict[str, Any]],
        *,
        external_id_field: str | None = None,
        poll_interval: float = 2.0,
        timeout: float = 900.0,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        """Bulk API 2.0 ingest — the correct path for large data mutations.

        This exists so that "update every Account in California" never becomes
        thousands of records travelling through the model. The caller builds
        the row set from a query; only the plan and the counts come back.

        Returns the real Salesforce job state plus a sample of failed rows.
        Nothing here reports success on the strength of an accepted upload:
        the job is polled to completion and the failure count is read back.
        """
        if not rows:
            return {
                "job_id": None,
                "state": "NotStarted",
                "records_processed": 0,
                "records_failed": 0,
                "note": "No rows were supplied, so no job was created.",
            }

        body: dict[str, Any] = {
            "object": sobject,
            "operation": operation,
            "lineEnding": "LF",
        }
        if operation == "upsert":
            if not external_id_field:
                raise SalesforceError(
                    error_type="MISSING_EXTERNAL_ID",
                    message="An upsert job requires externalIdFieldName.",
                    suggested_action="Name the external id field, or use 'update'.",
                )
            body["externalIdFieldName"] = external_id_field

        job = await self.request("POST", f"{self.base}/jobs/ingest", json=body)
        job_id = job["id"]

        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})

        try:
            await self.request(
                "PUT",
                f"{self.base}/jobs/ingest/{job_id}/batches",
                content=buffer.getvalue().encode("utf-8"),
                headers={"Content-Type": "text/csv"},
                expect_json=False,
            )
            await self.request(
                "PATCH",
                f"{self.base}/jobs/ingest/{job_id}",
                json={"state": "UploadComplete"},
            )
        except SalesforceError:
            # Abort rather than leave a half-loaded job sitting open in the org.
            await self._abort_job(job_id)
            raise

        deadline = time.monotonic() + timeout
        state = "UploadComplete"
        info: dict[str, Any] = {}
        while state not in {"JobComplete", "Failed", "Aborted"}:
            if time.monotonic() > deadline:
                return {
                    "job_id": job_id,
                    "state": state,
                    "timed_out": True,
                    "records_processed": info.get("numberRecordsProcessed", 0),
                    "records_failed": info.get("numberRecordsFailed", 0),
                    "note": (
                        f"Job {job_id} was still {state} after {timeout:.0f}s and is "
                        "still running in Salesforce."
                    ),
                }
            await asyncio.sleep(poll_interval)
            info = await self.request("GET", f"{self.base}/jobs/ingest/{job_id}")
            state = str(info.get("state") or "")
            if on_progress is not None:
                await on_progress(
                    {
                        "job_id": job_id,
                        "state": state,
                        "processed": info.get("numberRecordsProcessed", 0),
                        "failed": info.get("numberRecordsFailed", 0),
                    }
                )

        failures = await self._failed_rows(job_id) if info.get("numberRecordsFailed") else []
        return {
            "job_id": job_id,
            "state": state,
            "records_processed": info.get("numberRecordsProcessed", 0),
            "records_failed": info.get("numberRecordsFailed", 0),
            "failures_sample": failures[:20],
            "success": state == "JobComplete" and not info.get("numberRecordsFailed"),
        }

    async def _failed_rows(self, job_id: str) -> list[dict[str, Any]]:
        try:
            resp = await self.request(
                "GET",
                f"{self.base}/jobs/ingest/{job_id}/failedResults/",
                headers={"Accept": "text/csv"},
                expect_json=False,
            )
        except SalesforceError:  # pragma: no cover - best effort diagnostics
            return []
        text = resp.text if hasattr(resp, "text") else str(resp)
        return list(csv.DictReader(io.StringIO(text)))[:100]

    async def _abort_job(self, job_id: str) -> None:
        try:
            await self.request(
                "PATCH", f"{self.base}/jobs/ingest/{job_id}", json={"state": "Aborted"}
            )
        except SalesforceError:  # pragma: no cover - best effort cleanup
            log.warning("salesforce.bulk_abort_failed", job_id=job_id)

    async def count(self, sobject: str, where: str = "") -> int:
        """COUNT() for an object, used to size an operation before running it."""
        clause = f" WHERE {where}" if where else ""
        data = await self.query(f"SELECT COUNT() FROM {sobject}{clause}")
        return int(data.get("totalSize") or 0)
