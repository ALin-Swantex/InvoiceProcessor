from pathlib import Path

import httpx
import pytest

from app.outlook_graph import (
    OutlookConfigurationError,
    OutlookGraphClient,
    OutlookGraphError,
    OutlookSettings,
)


def settings(tmp_path: Path) -> OutlookSettings:
    return OutlookSettings(
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret="client-secret",
        mailbox="invoices@example.test",
        download_directory=tmp_path,
    )


def graph_client(
    tmp_path: Path, handler: httpx.MockTransport
) -> OutlookGraphClient:
    return OutlookGraphClient(
        settings(tmp_path),
        token_provider=lambda: "test-token",
        http_client=httpx.Client(transport=handler),
    )


def test_lists_unread_messages_with_attachments(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.params["$filter"] == (
            "hasAttachments eq true and isRead eq false"
        )
        assert "$orderby" not in request.url.params
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "message-1",
                        "subject": "Invoice",
                        "hasAttachments": True,
                    }
                ]
            },
        )

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    messages = client.list_invoice_emails(limit=10)

    assert messages[0]["id"] == "message-1"


def test_gets_one_invoice_email_by_message_id(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/message-1")
        assert "receivedDateTime" in request.url.params["$select"]
        return httpx.Response(
            200,
            json={
                "id": "message-1",
                "subject": "Invoice 1001",
                "from": {
                    "emailAddress": {
                        "name": "Supplier",
                        "address": "supplier@example.test",
                    }
                },
                "receivedDateTime": "2026-04-06T10:00:00Z",
            },
        )

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    message = client.get_invoice_email("message-1")

    assert message["subject"] == "Invoice 1001"


def test_filters_pdf_attachments(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "pdf-1",
                        "name": "invoice.pdf",
                        "contentType": "application/pdf",
                        "isInline": False,
                    },
                    {
                        "id": "image-1",
                        "name": "logo.png",
                        "contentType": "image/png",
                        "isInline": True,
                    },
                ]
            },
        )

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    attachments = client.list_pdf_attachments("message-1")

    assert [attachment["id"] for attachment in attachments] == ["pdf-1"]


def test_lists_pdf_and_excel_invoice_attachments(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "pdf", "name": "one.pdf", "isInline": False},
                    {"id": "xls", "name": "two.xls", "isInline": False},
                    {"id": "xlsx", "name": "three.xlsx", "isInline": False},
                    {"id": "macro", "name": "four.xlsm", "isInline": False},
                    {"id": "logo", "name": "logo.png", "isInline": False},
                ]
            },
        )

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    attachments = client.list_invoice_attachments("message-1")

    assert [attachment["id"] for attachment in attachments] == [
        "pdf",
        "xls",
        "xlsx",
    ]


def test_converts_excel_attachment_to_pdf_and_deletes_temporary_file(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/attachments/excel-1/$value"):
            return httpx.Response(200, content=b"excel workbook bytes")
        if request.method == "PUT":
            assert "/root:/Invoice%20Conversion/" in str(request.url)
            return httpx.Response(201, json={"id": "temporary-item"})
        if request.url.path.endswith("/items/temporary-item/content"):
            assert request.url.params["format"] == "pdf"
            return httpx.Response(200, content=b"%PDF-1.7\nconverted\n%%EOF")
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Graph request: {request.method} {request.url}")

    configured = settings(tmp_path)
    configured = OutlookSettings(
        tenant_id=configured.tenant_id,
        client_id=configured.client_id,
        client_secret=configured.client_secret,
        mailbox=configured.mailbox,
        download_directory=configured.download_directory,
        excel_conversion_drive_id="conversion-drive",
    )
    client = OutlookGraphClient(
        configured,
        token_provider=lambda: "test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    converted = client.download_invoice_attachment(
        "message-1", "excel-1", "supplier invoice.xlsx"
    )

    assert converted.name == "supplier invoice.pdf"
    assert converted.read_bytes().startswith(b"%PDF-")
    assert requests[-1] == (
        "DELETE",
        "/v1.0/drives/conversion-drive/items/temporary-item",
    )


def test_excel_conversion_requires_a_drive(tmp_path: Path) -> None:
    client = graph_client(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"workbook")
        ),
    )

    with pytest.raises(OutlookConfigurationError, match="EXCEL_CONVERSION_DRIVE_ID"):
        client.download_invoice_attachment(
            "message-1", "excel-1", "invoice.xlsx"
        )


def test_failed_excel_conversion_still_deletes_temporary_file(
    tmp_path: Path,
) -> None:
    deleted = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal deleted
        if request.url.path.endswith("/attachments/excel-1/$value"):
            return httpx.Response(200, content=b"excel workbook bytes")
        if request.method == "PUT":
            return httpx.Response(201, json={"id": "temporary-item"})
        if request.url.path.endswith("/items/temporary-item/content"):
            return httpx.Response(
                500,
                headers={"request-id": "conversion-failed"},
            )
        if request.method == "DELETE":
            deleted = True
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Graph request: {request.method} {request.url}")

    configured = settings(tmp_path)
    configured = OutlookSettings(
        tenant_id=configured.tenant_id,
        client_id=configured.client_id,
        client_secret=configured.client_secret,
        mailbox=configured.mailbox,
        download_directory=configured.download_directory,
        excel_conversion_drive_id="conversion-drive",
    )
    client = OutlookGraphClient(
        configured,
        token_provider=lambda: "test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(OutlookGraphError, match="conversion-failed"):
        client.download_invoice_attachment(
            "message-1", "excel-1", "invoice.xlsx"
        )

    assert deleted is True


def test_downloads_valid_pdf_without_overwriting(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4\ninvoice\n%%EOF")

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    first = client.download_pdf_attachment("message-1", "pdf-1", "invoice.pdf")
    second = client.download_pdf_attachment("message-1", "pdf-1", "invoice.pdf")

    assert first.name == "invoice.pdf"
    assert second.name == "invoice_2.pdf"
    assert first.read_bytes().startswith(b"%PDF-")


def test_rejects_non_pdf_attachment_content(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not a PDF")

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    with pytest.raises(OutlookGraphError, match="not a valid PDF"):
        client.download_pdf_attachment("message-1", "file-1", "invoice.pdf")


def test_surfaces_graph_failure_without_credentials(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"request-id": "graph-request-123"},
            json={"error": {"message": "Forbidden"}},
        )

    client = graph_client(tmp_path, httpx.MockTransport(respond))

    with pytest.raises(OutlookGraphError, match="graph-request-123"):
        client.list_invoice_emails()
