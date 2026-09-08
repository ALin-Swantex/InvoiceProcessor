import json

import httpx
from fastapi.testclient import TestClient
from pathlib import Path
from typing import cast

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.company_folders import (
    CompanyFolderStructure,
    discover_company_folder_structures,
)
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_graph import OutlookSettings
from app.outlook_notifications import OutlookNotificationStore
from app.sharepoint import SharePointClient, SharePointError, SharePointSettings
from app.sharepoint_intake import SharePointIncomingMonitor
from app.suppliers import SupplierStore
from tests.pdf_helpers import VALID_PDF_BYTES


def _client(handler: httpx.MockTransport) -> SharePointClient:
    return SharePointClient(
        SharePointSettings(
            site_id="site",
            drive_id="drive",
            incoming_folder="Invoices/Incoming",
        ),
        OutlookSettings(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            mailbox="invoices@example.test",
            download_directory=Path("downloads"),
        ),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=handler),
    )


def test_lists_nested_sharepoint_folder_paths_with_pagination() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "archive",
                            "name": "Archive",
                            "folder": {},
                            "parentReference": {"path": "/drives/drive/root:"},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "invoices",
                        "name": "Invoices",
                        "folder": {},
                        "parentReference": {"path": "/drives/drive/root:"},
                    },
                    {
                        "id": "nominal",
                        "name": "Nominal",
                        "folder": {},
                        "parentReference": {
                            "path": "/drives/drive/root:/Invoices"
                        },
                    },
                    {
                        "id": "po",
                        "name": "PO Matching",
                        "folder": {},
                        "parentReference": {
                            "path": "/drives/drive/root:/Invoices"
                        },
                    },
                    {
                        "id": "pdf",
                        "name": "invoice.pdf",
                        "file": {},
                        "parentReference": {
                            "path": "/drives/drive/root:/Invoices"
                        },
                    },
                ],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/drives/drive/"
                    "root/delta?page=2"
                ),
            },
        )

    client = _client(httpx.MockTransport(respond))

    assert client.list_folder_paths() == [
        "Archive",
        "Invoices",
        "Invoices/Nominal",
        "Invoices/PO Matching",
    ]


def test_folder_listing_surfaces_graph_error() -> None:
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                403, headers={"request-id": "forbidden-request"}
            )
        )
    )

    try:
        client.list_folder_paths()
    except SharePointError as error:
        assert "HTTP 403" in str(error)
        assert "forbidden-request" in str(error)
    else:
        raise AssertionError("Expected SharePointError")


def test_move_resolves_existing_folders_without_unsupported_graph_filter() -> None:
    requested_paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert "$filter" not in request.url.params
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {
                "parentReference": {"id": "nominal"},
                "name": "IRJ000001-test.pdf",
            }
            return httpx.Response(
                200,
                json={"id": "pdf-1", "name": "IRJ000001-test.pdf"},
            )
        children = {
            "/v1.0/drives/drive/root/children": {
                "value": [{"id": "invoices", "name": "Invoices", "folder": {}}]
            },
            "/v1.0/drives/drive/items/invoices/children": {
                "value": [{"id": "gifted", "name": "GIFTED", "folder": {}}]
            },
            "/v1.0/drives/drive/items/gifted/children": {
                "value": [
                    {
                        "id": "nominal",
                        "name": "Nominal Invoices",
                        "folder": {},
                    }
                ]
            },
        }
        return httpx.Response(200, json=children[request.url.path])

    client = _client(httpx.MockTransport(respond))

    moved = client.move_to_folder(
        "pdf-1",
        "Invoices/GIFTED/Nominal Invoices",
        "IRJ000001-test.pdf",
    )

    assert moved["name"] == "IRJ000001-test.pdf"
    assert requested_paths[-1] == "/v1.0/drives/drive/items/pdf-1"


def test_lists_and_downloads_incoming_pdfs_with_bearer_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=VALID_PDF_BYTES)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "pdf-1",
                        "name": "invoice.pdf",
                        "size": 24,
                        "file": {"mimeType": "application/pdf"},
                        "webUrl": "https://sharepoint.example/invoice.pdf",
                    },
                    {
                        "id": "sheet-1",
                        "name": "notes.xlsx",
                        "file": {
                            "mimeType": (
                                "application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet"
                            )
                        },
                    },
                ]
            },
        )

    client = _client(httpx.MockTransport(respond))

    assert [item["id"] for item in client.list_incoming_pdfs()] == ["pdf-1"]
    assert client.download_item("pdf-1").startswith(b"%PDF-")


class FakeIncomingClient:
    def __init__(self) -> None:
        self.item = {
            "id": "drive-item-1",
            "name": "supplier-invoice.pdf",
            "size": 24,
            "file": {"mimeType": "application/pdf"},
            "webUrl": "https://sharepoint.example/supplier-invoice.pdf",
            "createdDateTime": "2026-09-08T10:00:00Z",
        }
        self.uploads: list[tuple[str, bytes]] = []

    def list_incoming_pdfs(self) -> list[dict[str, object]]:
        return [self.item]

    def download_item(self, item_id: str) -> bytes:
        assert item_id == "drive-item-1"
        return VALID_PDF_BYTES

    def upload_to_incoming(
        self, filename: str, content: bytes
    ) -> dict[str, object]:
        self.uploads.append((filename, content))
        return self.item

    def get_item_web_url(self, item: dict[str, object]) -> str | None:
        value = item.get("webUrl")
        return value if isinstance(value, str) else None


def test_incoming_monitor_registers_each_drive_item_once(tmp_path: Path) -> None:
    store = InvoiceStore(tmp_path / "invoices.db")
    fake_client = FakeIncomingClient()
    extracted: list[int] = []
    monitor = SharePointIncomingMonitor(
        cast(SharePointClient, fake_client),
        store,
        cache_directory=tmp_path / "cache",
        extraction_runner=extracted.append,
    )

    assert monitor.scan_once() == 1
    assert monitor.scan_once() == 0

    records = store.list()
    assert len(records) == 1
    assert records[0].sharepoint_item_id == "drive-item-1"
    assert records[0].sharepoint_web_url == (
        "https://sharepoint.example/supplier-invoice.pdf"
    )
    assert Path(records[0].stored_path).read_bytes().startswith(b"%PDF-")
    assert extracted == [records[0].id]


def test_manual_upload_uses_sharepoint_incoming_as_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.main.ai_extraction_configured", lambda: False)
    fake_client = FakeIncomingClient()
    app = create_app(
        invoice_store=InvoiceStore(tmp_path / "invoices.db"),
        auth_store=AuthStore(tmp_path / "auth.db"),
        activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
        notification_store=OutlookNotificationStore(tmp_path / "notifications.db"),
        sharepoint_client=cast(SharePointClient, fake_client),
    )
    client = TestClient(app)
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("supplier-invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    assert fake_client.uploads == [
        ("supplier-invoice.pdf", VALID_PDF_BYTES)
    ]
    assert response.json()["sharepoint_item_id"] == "drive-item-1"


def test_manual_upload_rejects_malformed_pdf_before_sharepoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.main.ai_extraction_configured", lambda: False)
    fake_client = FakeIncomingClient()
    app = create_app(
        invoice_store=InvoiceStore(tmp_path / "invoices.db"),
        auth_store=AuthStore(tmp_path / "auth.db"),
        activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
        notification_store=OutlookNotificationStore(tmp_path / "notifications.db"),
        sharepoint_client=cast(SharePointClient, fake_client),
    )
    client = TestClient(app)
    client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )

    response = client.post(
        "/api/invoices/manual-upload",
        files={
            "file": (
                "broken.pdf",
                b"%PDF-1.4\n%%EOF",
                "application/pdf",
            )
        },
    )

    assert response.status_code == 400
    assert "malformed" in response.json()["detail"]
    assert fake_client.uploads == []


class FakeSharePointClient:
    def list_folder_paths(self) -> list[str]:
        structure = CompanyFolderStructure.from_root("Invoices/Acme")
        return [
            "Invoices",
            "Invoices/Incoming Invoices",
            "Invoices/Rejected Invoices",
            *structure.as_dict().values(),
        ]


def test_admin_can_load_sharepoint_folder_options(tmp_path) -> None:
    app = create_app(
        invoice_store=InvoiceStore(tmp_path / "invoices.db"),
        auth_store=AuthStore(tmp_path / "auth.db"),
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
        activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
        notification_store=OutlookNotificationStore(tmp_path / "notifications.db"),
        sharepoint_client=cast(SharePointClient, FakeSharePointClient()),
    )
    client = TestClient(app)
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "ChangeMe-Admin1!"},
    ).status_code == 200

    response = client.get("/api/admin/sharepoint/folders")

    assert response.status_code == 200
    body = response.json()
    assert body["company_roots"][0]["root"] == "Invoices/Acme"
    assert body["company_roots"][0]["nominal_invoices"] == (
        "Invoices/Acme/Nominal Invoices"
    )
    assert body["company_roots"][0]["po_match"] == (
        "Invoices/Acme/PO Invoices/PO Match"
    )
    assert body["shared_folders"] == {
        "incoming": "Invoices/Incoming Invoices",
        "rejected": "Invoices/Rejected Invoices",
    }
    valid_company = client.post(
        "/api/admin/companies",
        json={
            "name": "Acme",
            "sharepoint_root_folder": "Invoices/Acme",
        },
    )
    invalid_company = client.post(
        "/api/admin/companies",
        json={
            "name": "Invalid",
            "sharepoint_root_folder": "Invoices/Invalid",
        },
    )

    assert valid_company.status_code == 200
    assert valid_company.json()["company_folder"] == (
        "Invoices/Acme/Nominal Invoices"
    )
    assert valid_company.json()["po_matching_folder"] == (
        "Invoices/Acme/PO Invoices/PO Match"
    )
    assert invalid_company.status_code == 400
    assert "complete company folder structure" in invalid_company.json()["detail"]


def test_incomplete_company_folder_is_not_offered() -> None:
    folders = [
        "Invoices",
        "Invoices/Complete",
        "Invoices/Complete/Nominal Invoices",
        "Invoices/Incomplete",
    ]

    assert discover_company_folder_structures(folders) == []


def test_discovers_all_verified_company_folder_structures() -> None:
    company_codes = ("CEL", "GBCC", "GIFTED", "LING", "PK", "SWAN")
    folders = [
        "Invoices",
        "Invoices/Incoming Invoices",
        "Invoices/Rejected Invoices",
    ]
    for code in company_codes:
        folders.extend(
            CompanyFolderStructure.from_root(f"Invoices/{code}").as_dict().values()
        )

    structures = discover_company_folder_structures(folders)

    assert [structure.root for structure in structures] == [
        f"Invoices/{code}" for code in company_codes
    ]
