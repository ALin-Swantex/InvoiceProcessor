from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.auth import AuthStore
from app.main import create_app
from app.outlook_graph import OutlookGraphClient, OutlookSettings
from app.outlook_notifications import OutlookNotificationStore


CLIENT_STATE = "test-client-state"


def webhook_client(
    tmp_path: Path,
) -> tuple[TestClient, OutlookNotificationStore]:
    store = OutlookNotificationStore(tmp_path / "notifications.db")
    client = TestClient(
        create_app(
            notification_store=store,
            webhook_client_state=CLIENT_STATE,
            auth_store=AuthStore(tmp_path / "auth.db"),
        )
    )
    return client, store


def notification_payload() -> dict[str, object]:
    return {
        "value": [
            {
                "subscriptionId": "subscription-1",
                "changeType": "created",
                "clientState": CLIENT_STATE,
                "resource": "Users/mailbox/Messages/message-1",
                "resourceData": {"id": "message-1"},
            }
        ]
    }


def test_returns_graph_validation_token_as_plain_text(tmp_path: Path) -> None:
    client, _ = webhook_client(tmp_path)

    response = client.post(
        "/api/outlook/notifications",
        params={"validationToken": "graph-validation-token"},
    )

    assert response.status_code == 200
    assert response.text == "graph-validation-token"
    assert response.headers["content-type"].startswith("text/plain")


def test_valid_notification_is_queued_and_acknowledged(tmp_path: Path) -> None:
    client, store = webhook_client(tmp_path)

    response = client.post(
        "/api/outlook/notifications", json=notification_payload()
    )

    assert response.status_code == 202
    queued = store.list()
    assert len(queued) == 1
    assert queued[0].message_id == "message-1"
    assert queued[0].status == "pending"


def test_duplicate_notification_is_idempotent(tmp_path: Path) -> None:
    client, store = webhook_client(tmp_path)
    payload = notification_payload()

    assert client.post("/api/outlook/notifications", json=payload).status_code == 202
    assert client.post("/api/outlook/notifications", json=payload).status_code == 202

    assert len(store.list()) == 1


def test_rejects_invalid_client_state(tmp_path: Path) -> None:
    client, store = webhook_client(tmp_path)
    payload = notification_payload()
    payload["value"][0]["clientState"] = "wrong-state"  # type: ignore[index]

    response = client.post("/api/outlook/notifications", json=payload)

    assert response.status_code == 401
    assert store.list() == []


def test_lifecycle_notification_validates_client_state(tmp_path: Path) -> None:
    client, _ = webhook_client(tmp_path)

    accepted = client.post(
        "/api/outlook/lifecycle",
        json={
            "value": [
                {
                    "subscriptionId": "subscription-1",
                    "clientState": CLIENT_STATE,
                    "lifecycleEvent": "reauthorizationRequired",
                }
            ]
        },
    )
    rejected = client.post(
        "/api/outlook/lifecycle",
        json={
            "value": [
                {
                    "subscriptionId": "subscription-1",
                    "clientState": "wrong-state",
                    "lifecycleEvent": "reauthorizationRequired",
                }
            ]
        },
    )

    assert accepted.status_code == 202
    assert rejected.status_code == 401


def graph_settings(tmp_path: Path) -> OutlookSettings:
    return OutlookSettings(
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret="client-secret",
        mailbox="invoices@example.test",
        download_directory=tmp_path,
    )


def test_creates_inbox_subscription_with_safe_expiration(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        assert request.method == "POST"
        assert request.url.path.endswith("/subscriptions")
        return httpx.Response(201, json={"id": "subscription-1"})

    client = OutlookGraphClient(
        graph_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
    )

    result = client.create_inbox_subscription(
        notification_url="https://example.test/api/outlook/notifications",
        lifecycle_notification_url="https://example.test/api/outlook/lifecycle",
        client_state=CLIENT_STATE,
    )

    assert result["id"] == "subscription-1"
    assert captured["changeType"] == "created"
    assert captured["resource"] == (
        "/users/invoices@example.test/mailFolders('inbox')/messages"
    )
    assert captured["clientState"] == CLIENT_STATE
    assert str(captured["expirationDateTime"]).endswith("Z")


def test_renews_existing_subscription(tmp_path: Path) -> None:
    def capture(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path.endswith("/subscriptions/subscription-1")
        return httpx.Response(200, json={"id": "subscription-1"})

    client = OutlookGraphClient(
        graph_settings(tmp_path),
        token_provider=lambda: "token",
        http_client=httpx.Client(transport=httpx.MockTransport(capture)),
    )

    result = client.renew_subscription("subscription-1")

    assert result["id"] == "subscription-1"
