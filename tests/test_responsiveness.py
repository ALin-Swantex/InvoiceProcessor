"""Phase 4 responsiveness fixes: activity cursor, compression and caching,
statement id cache, and manual upload error handling."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.config_db import SQLiteProcessConfigurationStore
from app.invoice_lifecycle import InvoiceExtractionUnavailableError
from app.invoices import InvoiceStore
from app.main import _INDEX_HTML_CACHE, _index_html, create_app
from app.outlook_notifications import OutlookNotificationStore
from app.sharepoint import SharePointClient
from app.suppliers import SupplierStore
from tests.pdf_helpers import VALID_PDF_BYTES


def _client(tmp_path: Path, sharepoint_client: object | None = None) -> TestClient:
    return TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
            process_configuration_store=SQLiteProcessConfigurationStore(
                tmp_path / "config.db"
            ),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "outlook_notifications.db"
            ),
            sharepoint_client=cast(SharePointClient, sharepoint_client),
            auto_configure_sharepoint=False,
        )
    )


def _login(client: TestClient, username: str = "purchase.ledger",
           password: str = "ChangeMe-PL1!") -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


def test_activity_latest_returns_newest_id_without_changing_activity(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _login(client)
    feed = client.app.state.activity_feed
    ids = [
        feed.add_event(
            event_type="manual_intake",
            target_role="all",
            message=f"event {index}",
        ).id
        for index in range(60)
    ]

    latest = client.get("/api/activity/latest")
    assert latest.status_code == 200
    assert latest.json() == {"latest_id": max(ids)}

    # /api/activity keeps its existing oldest-first, 50-item behaviour.
    history = client.get("/api/activity").json()
    assert [event["id"] for event in history] == sorted(ids)[:50]
    # A cursor at the latest id has nothing to replay.
    assert client.get(
        "/api/activity", params={"since_id": max(ids)}
    ).json() == []


def test_activity_latest_requires_login(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/activity/latest").status_code == 401


def test_static_assets_are_gzipped_and_revalidate(tmp_path: Path) -> None:
    client = _client(tmp_path)

    script = client.get("/static/js/sections.js", headers={"Accept-Encoding": "gzip"})
    assert script.status_code == 200
    assert script.headers["content-encoding"] == "gzip"
    assert script.headers["cache-control"] == "no-cache"
    etag = script.headers["etag"]

    revalidated = client.get(
        "/static/js/sections.js",
        headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
    )
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["cache-control"] == "no-cache"

    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["content-encoding"] == "gzip"


def test_index_html_is_cached_by_mtime() -> None:
    first = _index_html()
    cached_mtime = _INDEX_HTML_CACHE["mtime_ns"]
    assert cached_mtime is not None
    assert _index_html() is first


def test_event_streams_are_not_gzipped(tmp_path: Path) -> None:
    client = _client(tmp_path)

    def finite_events():
        for index in range(50):
            yield f"event: invoice-update\ndata: {index}\n\n" + ("x" * 100)

    # Same middleware stack as /api/activity/stream, but a finite body so
    # the test cannot hang on the real (infinite) stream.
    client.app.router.add_route(
        "/__test__/stream",
        lambda request: StreamingResponse(
            finite_events(), media_type="text/event-stream"
        ),
    )
    response = client.get("/__test__/stream", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "data: 49" in response.text


class _CountingStatementClient:
    def __init__(self) -> None:
        self.list_calls = 0
        self.statements = [{"id": "statement-1", "name": "September.pdf"}]

    def list_statement_library(self):
        self.list_calls += 1
        return {"Acme Trading Ltd": list(self.statements)}

    def download_item(self, item_id: str) -> bytes:
        return VALID_PDF_BYTES


def test_statement_pdf_reuses_recent_listing_and_refreshes_on_miss(
    tmp_path: Path,
) -> None:
    fake = _CountingStatementClient()
    client = _client(tmp_path, fake)
    _login(client)

    assert client.get("/api/statements").status_code == 200
    assert fake.list_calls == 1
    for _ in range(3):
        assert client.get("/api/statements/statement-1/pdf").status_code == 200
    assert fake.list_calls == 1

    # A statement filed after the cached listing still opens.
    fake.statements.append({"id": "statement-2", "name": "October.pdf"})
    assert client.get("/api/statements/statement-2/pdf").status_code == 200
    assert fake.list_calls == 2

    # Unknown ids are still rejected (after one fresh listing).
    assert client.get("/api/statements/unknown/pdf").status_code == 404
    assert fake.list_calls == 3


def test_statement_pdf_cache_expires(tmp_path: Path, monkeypatch) -> None:
    import app.routers.statements as statements

    fake = _CountingStatementClient()
    client = _client(tmp_path, fake)
    _login(client)
    assert client.get("/api/statements/statement-1/pdf").status_code == 200
    assert fake.list_calls == 1
    monkeypatch.setattr(statements, "STATEMENT_ID_CACHE_TTL_SECONDS", -1.0)
    assert client.get("/api/statements/statement-1/pdf").status_code == 200
    assert fake.list_calls == 2


def test_manual_upload_reports_upload_failure_instead_of_crashing(
    tmp_path: Path,
) -> None:
    class FailingUploadClient:
        def upload_to_incoming(self, filename: str, content: bytes):
            raise InvoiceExtractionUnavailableError("extraction offline")

    client = _client(tmp_path, FailingUploadClient())
    _login(client)

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("manual.pdf", VALID_PDF_BYTES, "application/pdf")},
    )

    # Previously ``item`` was unbound here and the request crashed (500).
    assert response.status_code == 503
    assert "SharePoint Incoming upload failed" in response.json()["detail"]


def test_unreachable_database_returns_a_clear_503(tmp_path) -> None:
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    from tests.test_api import client_for

    client = client_for(tmp_path)

    @client.app.get("/api/_test/pool-timeout")
    def pool_timeout() -> None:
        raise PoolTimeout("couldn't get a connection after 30.00 sec")

    @client.app.get("/api/_test/connect-failed")
    def connect_failed() -> None:
        raise OperationalError("failed to resolve host")

    for path in ("/api/_test/pool-timeout", "/api/_test/connect-failed"):
        response = client.get(path)
        assert response.status_code == 503
        assert "database is unreachable" in response.json()["detail"]
