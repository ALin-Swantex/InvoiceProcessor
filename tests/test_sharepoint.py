import httpx
from fastapi.testclient import TestClient
from pathlib import Path
from typing import cast

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_graph import OutlookSettings
from app.outlook_notifications import OutlookNotificationStore
from app.sharepoint import SharePointClient, SharePointError, SharePointSettings
from app.suppliers import SupplierStore


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
        path = request.url.path
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={"value": [{"id": "archive", "name": "Archive", "folder": {}}]},
            )
        if path.endswith("/root/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "invoices", "name": "Invoices", "folder": {}},
                    ],
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/drives/drive/"
                        "root/children?page=2"
                    ),
                },
            )
        if path.endswith("/items/invoices/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "nominal", "name": "Nominal", "folder": {}},
                        {"id": "po", "name": "PO Matching", "folder": {}},
                        {"id": "pdf", "name": "invoice.pdf", "file": {}},
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})

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


class FakeSharePointClient:
    def list_folder_paths(self) -> list[str]:
        return ["Invoices/Acme/Nominal", "Invoices/Acme/PO Matching"]


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
    assert response.json()["folders"] == [
        "Invoices/Acme/Nominal",
        "Invoices/Acme/PO Matching",
    ]
    valid_company = client.post(
        "/api/admin/companies",
        json={
            "name": "Acme",
            "company_folder": "Invoices/Acme/Nominal",
            "po_matching_folder": "Invoices/Acme/PO Matching",
        },
    )
    invalid_company = client.post(
        "/api/admin/companies",
        json={
            "name": "Invalid",
            "company_folder": "Invoices/Acme/Typo",
            "po_matching_folder": "Invoices/Acme/PO Matching",
        },
    )

    assert valid_company.status_code == 200
    assert invalid_company.status_code == 400
    assert "existing SharePoint folders" in invalid_company.json()["detail"]
