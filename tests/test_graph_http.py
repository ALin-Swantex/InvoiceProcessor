from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.email_notifications import (
    EmailNotificationError,
    EmailNotificationSettings,
    GraphEmailNotifier,
)
from app.graph_http import GraphSession, parse_retry_after, shared_token_provider
from app.outlook_graph import (
    OutlookGraphClient,
    OutlookGraphError,
    OutlookSettings,
    default_token_provider,
)
from app.sharepoint import SharePointClient, SharePointError, SharePointSettings


class Boom(RuntimeError):
    pass


def outlook_settings(tmp_path: Path, **overrides: object) -> OutlookSettings:
    values: dict[str, object] = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "mailbox": "invoices@example.test",
        "download_directory": tmp_path,
    }
    values.update(overrides)
    return OutlookSettings(**values)  # type: ignore[arg-type]


def session(handler, sleeps: list[float], **kwargs: object) -> GraphSession:
    return GraphSession(
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: "token",
        error_class=Boom,
        sleep=sleeps.append,
        jitter=lambda ceiling: 0.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_retries_429_honouring_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    response = session(respond, sleeps).request("GET", "/me")

    assert response.json() == {"ok": True}
    assert calls == 2
    assert sleeps == [7.0]


def test_retry_after_is_capped() -> None:
    sleeps: list[float] = []
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)]
    )

    session(lambda request: next(responses), sleeps, retry_after_cap_seconds=60).request(
        "GET", "/me"
    )

    assert sleeps == [60]


def test_retries_503_with_exponential_backoff_then_raises_with_request_id() -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"request-id": f"req-{calls}"})

    with pytest.raises(Boom, match=r"Listing failed with HTTP 503; request ID: req-4\."):
        session(respond, sleeps, max_retries=3).request(
            "GET", "/me", operation="Listing"
        )

    assert calls == 4
    # Equal jitter with zero spread: half of 0.5, 1, 2 seconds.
    assert sleeps == [0.25, 0.5, 1.0]


def test_does_not_retry_client_errors() -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, headers={"request-id": "missing"})

    with pytest.raises(Boom, match="HTTP 404; request ID: missing"):
        session(respond, sleeps).request("GET", "/me")

    assert calls == 1
    assert sleeps == []


def test_non_idempotent_post_is_not_retried_on_503_but_is_on_429() -> None:
    sleeps: list[float] = []
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(Boom):
        session(respond, sleeps).request("POST", "/things", json={})
    assert calls == 1

    responses = iter([httpx.Response(429), httpx.Response(201)])
    created = session(lambda request: next(responses), sleeps).request(
        "POST", "/things", json={}
    )
    assert created.status_code == 201


def test_retries_idempotent_read_timeout_but_not_for_post() -> None:
    sleeps: list[float] = []
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200)

    assert session(respond, sleeps).request("GET", "/me").status_code == 200
    assert calls == 2

    def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(httpx.ReadTimeout):
        session(always_timeout, sleeps).request("POST", "/sendMail", json={})


def test_retries_connect_error_even_for_post() -> None:
    sleeps: list[float] = []
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(202)

    assert session(respond, sleeps).request("POST", "/sendMail", json={}).status_code == 202
    assert calls == 2


def test_paginates_and_sends_params_only_on_first_page() -> None:
    seen: list[httpx.URL] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"value": [3]})
        return httpx.Response(
            200,
            json={
                "value": [1, 2],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/items?page=2",
            },
        )

    pages = list(session(respond, []).paginate("/items", params={"$top": "2"}))

    assert [page["value"] for page in pages] == [[1, 2], [3]]
    assert seen[0].params["$top"] == "2"
    assert "$top" not in seen[1].params


def test_streaming_download_stops_early_when_over_limit() -> None:
    produced: list[int] = []

    def body():
        for index in range(100):
            produced.append(index)
            yield b"x" * 10

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    with pytest.raises(Boom, match="too big"):
        session(respond, []).download(
            "/file",
            max_bytes=25,
            too_large_error=lambda: Boom("too big"),
            chunk_size=10,
        )

    assert len(produced) < 10


def test_download_rejects_declared_content_length_without_reading() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(Boom, match="too big"):
        session(respond, []).download(
            "/file", max_bytes=10, too_large_error=lambda: Boom("too big")
        )


def test_download_retries_503_then_returns_body() -> None:
    sleeps: list[float] = []
    responses = iter([httpx.Response(503), httpx.Response(200, content=b"%PDF-ok")])

    content = session(lambda request: next(responses), sleeps).download(
        "/file", max_bytes=100, too_large_error=lambda: Boom("too big")
    )

    assert content == b"%PDF-ok"
    assert len(sleeps) == 1


def test_parse_retry_after_http_date() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:10 GMT", now=now) == 10
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


def test_outlook_pdf_download_size_limit_uses_streaming(tmp_path: Path) -> None:
    produced = 0

    def body():
        nonlocal produced
        while True:
            produced += 1
            yield b"%PDF-" + b"x" * 1019

    client = OutlookGraphClient(
        outlook_settings(tmp_path, max_pdf_bytes=4096),
        token_provider=lambda: "token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body())
            )
        ),
    )

    with pytest.raises(OutlookGraphError, match="The PDF exceeds the configured size limit."):
        client.download_pdf_attachment("message", "attachment", "invoice.pdf")

    assert produced < 100
    assert list(tmp_path.iterdir()) == []


def test_outlook_client_retries_throttled_message_listing(
    tmp_path: Path, no_real_graph_retry_sleep: list[float]
) -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"value": [{"id": "message-1"}]}),
        ]
    )
    client = OutlookGraphClient(
        outlook_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: next(responses))
        ),
    )

    assert client.list_invoice_emails()[0]["id"] == "message-1"
    assert no_real_graph_retry_sleep == [2.0]


def test_sharepoint_incoming_listing_paginates_through_shared_helper(
    tmp_path: Path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200, json={"value": [{"id": "b", "name": "b.pdf", "file": {}}]}
            )
        return httpx.Response(
            200,
            json={
                "value": [{"id": "a", "name": "a.pdf", "file": {}}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?page=2",
            },
        )

    client = SharePointClient(
        SharePointSettings(site_id="s", drive_id="d", incoming_folder="Incoming"),
        outlook_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    assert [item["id"] for item in client.list_incoming_pdfs()] == ["a", "b"]


def test_sharepoint_rename_upload_is_not_retried_on_503(tmp_path: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"request-id": "upload-503"})

    client = SharePointClient(
        SharePointSettings(site_id="s", drive_id="d", incoming_folder="Incoming"),
        outlook_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(SharePointError, match="SharePoint upload failed with HTTP 503"):
        client.upload_to_incoming("a.pdf", b"%PDF-")
    assert calls == 1

    calls = 0
    with pytest.raises(SharePointError):
        client.upload_to_incoming("a.pdf", b"%PDF-", conflict_behavior="replace")
    assert calls == 4


def test_send_mail_is_not_retried_on_server_error(tmp_path: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"request-id": "mail-503"})

    notifier = GraphEmailNotifier(
        EmailNotificationSettings(sender_mailbox="sender@example.test", enabled=True),
        outlook_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(EmailNotificationError, match="HTTP 503; request ID: mail-503"):
        notifier.send(recipient="a@example.test", subject="s", body="b")
    assert calls == 1


def test_clients_share_one_token_provider_per_credential_set(tmp_path: Path) -> None:
    shared_token_provider.cache_clear()
    first = default_token_provider(outlook_settings(tmp_path))
    second = default_token_provider(outlook_settings(tmp_path / "other"))
    different = default_token_provider(outlook_settings(tmp_path, client_id="other"))

    assert first is second
    assert first is not different
    # Creating providers must not touch the network (MSAL discovers the
    # authority when its application object is built).
    assert first._application is None
    assert different._application is None
    shared_token_provider.cache_clear()
