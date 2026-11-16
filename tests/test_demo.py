import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import SEED_USERS
from app.demo import (
    _sample_invoices,
    build_demo_app,
    prepare_demo_environment,
    sample_pdf,
)
from app.pdf_validation import validate_pdf

PASSWORDS = {user[0]: user[4] for user in SEED_USERS}


@pytest.fixture(autouse=True)
def restore_environment():
    """Demo mode rewrites os.environ on purpose; never let that leak into other tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORDS[username]}
    )
    assert response.status_code == 200, response.text


def test_sample_pdf_is_a_valid_pdf() -> None:
    validate_pdf(sample_pdf("Heading (with) \\ specials", ["Supplier: Acme", "Total: 10"]))


def test_demo_app_signs_in_locally_and_shows_every_workflow_stage(
    tmp_path: Path,
) -> None:
    client = TestClient(build_demo_app(tmp_path / "demo"))
    _login(client, "admin")

    invoices = client.get("/api/invoices?limit=500")
    assert invoices.status_code == 200
    statuses = {invoice["status"] for invoice in invoices.json()}
    assert len(invoices.json()) == len(_sample_invoices("a@example.test", "b@example.test"))
    assert {
        "Awaiting PO Matching",
        "Awaiting Approval 1",
        "Approved for Payment - BACS",
        "Reconciled / Complete",
        "Rejected",
    } <= statuses

    pdf = client.get(f"/api/invoices/{invoices.json()[0]['id']}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"


def test_demo_approver_only_sees_their_assigned_invoices(tmp_path: Path) -> None:
    client = TestClient(build_demo_app(tmp_path / "demo"))
    _login(client, "jordan.blake")

    invoices = client.get("/api/invoices?limit=500").json()

    assert invoices, "the approver should see the invoices assigned to them"
    assert all(
        invoice["status"].startswith(("Awaiting Approval", "Approval Query"))
        or invoice["status"].startswith("Approved")
        for invoice in invoices
    )


def test_reusing_a_data_directory_does_not_seed_twice(tmp_path: Path) -> None:
    data_dir = tmp_path / "demo"
    first = TestClient(build_demo_app(data_dir))
    _login(first, "admin")
    count = len(first.get("/api/invoices?limit=500").json())

    second = TestClient(build_demo_app(data_dir))
    _login(second, "admin")

    assert len(second.get("/api/invoices?limit=500").json()) == count


def test_demo_environment_drops_every_cloud_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "GRAPH_CLIENT_SECRET",
        "OUTLOOK_MCP_CLIENT_SECRET",
        "AZURE_POSTGRES_HOST",
        "SHAREPOINT_SITE_ID",
        "DATABASE_URL",
        "SAGE_API_KEY",
    ):
        monkeypatch.setenv(name, "must-not-survive")

    prepare_demo_environment(tmp_path)

    for name in (
        "GRAPH_CLIENT_SECRET",
        "OUTLOOK_MCP_CLIENT_SECRET",
        "AZURE_POSTGRES_HOST",
        "SHAREPOINT_SITE_ID",
        "DATABASE_URL",
        "SAGE_API_KEY",
    ):
        assert name not in os.environ
    assert os.environ["AUTH_LOCAL_LOGIN_ENABLED"] == "true"
    assert os.environ["INVOICE_PROCESSOR_SKIP_DOTENV"] == "1"
