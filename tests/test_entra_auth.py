from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthError, AuthStore, User
from app.companies import CompanyStore
from app.entra_auth import EntraAuthClient, EntraAuthSettings
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.suppliers import SupplierStore


class FakeTokenClient:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims

    def acquire_token_by_auth_code_flow(self, flow, query_parameters):
        return {"id_token_claims": self.claims}


class FakeEntraClient:
    def initiate_flow(self) -> dict[str, object]:
        return {
            "state": "expected-state",
            "auth_uri": "https://login.microsoftonline.test/authorize",
        }

    def complete_flow(self, flow, query_parameters) -> User:
        assert flow["state"] == "expected-state"
        assert query_parameters["state"] == "expected-state"
        return User(
            username="ledger@swantex.com",
            display_name="Ledger User",
            email="ledger@swantex.com",
            role="purchase_ledger",
        )

    def logout_url(self) -> str:
        return "https://login.microsoftonline.test/logout"


def _entra_client(claims: dict[str, object]) -> EntraAuthClient:
    client = EntraAuthClient.__new__(EntraAuthClient)
    client.settings = EntraAuthSettings(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        redirect_uri="https://app.example.test/api/auth/microsoft/callback",
        post_logout_redirect_uri="https://app.example.test/",
    )
    client._client = FakeTokenClient(claims)
    return client


def test_entra_claim_maps_to_internal_role() -> None:
    user = _entra_client(
        {
            "preferred_username": "ledger@swantex.com",
            "name": "Ledger User",
            "roles": ["InvoiceProcessor.PurchaseLedger"],
        }
    ).complete_flow({}, {})

    assert user.username == "ledger@swantex.com"
    assert user.role == "purchase_ledger"


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ([], "has not been assigned"),
        (
            ["InvoiceProcessor.Admin", "InvoiceProcessor.PurchaseLedger"],
            "multiple Invoice Processor roles",
        ),
    ],
)
def test_entra_login_requires_exactly_one_role(
    roles: list[str], message: str
) -> None:
    with pytest.raises(AuthError, match=message):
        _entra_client(
            {
                "preferred_username": "user@swantex.com",
                "roles": roles,
            }
        ).complete_flow({}, {})


def test_microsoft_login_callback_creates_application_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_LOCAL_LOGIN_ENABLED", raising=False)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    client = TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "notifications.db"
            ),
            entra_auth_client=FakeEntraClient(),  # type: ignore[arg-type]
            auto_configure_sharepoint=False,
        )
    )

    config = client.get("/api/auth/config")
    assert config.json() == {
        "microsoft_enabled": True,
        "local_enabled": False,
    }
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "ChangeMe-Admin1!"},
    ).status_code == 404

    login = client.get(
        "/api/auth/microsoft/login", follow_redirects=False
    )
    assert login.status_code == 302
    assert login.headers["location"].endswith("/authorize")

    callback = client.get(
        "/api/auth/microsoft/callback",
        params={"state": "expected-state", "code": "code"},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert client.get("/api/auth/me").json()["role"] == "purchase_ledger"

    replay = client.get(
        "/api/auth/microsoft/callback",
        params={"state": "expected-state", "code": "code"},
        follow_redirects=False,
    )
    assert replay.status_code == 400
