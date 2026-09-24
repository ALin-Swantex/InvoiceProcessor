import os

import pytest


# Test modules import app.main during collection. Define these before those
# imports so load_dotenv() cannot activate real Entra credentials or initiate
# tenant discovery during an offline test run.
os.environ["AUTH_ENTRA_TENANT_ID"] = ""
os.environ["AUTH_ENTRA_CLIENT_ID"] = ""
os.environ["AUTH_ENTRA_CLIENT_SECRET"] = ""
os.environ["AUTH_ENTRA_REDIRECT_URI"] = ""
os.environ["AUTH_LOCAL_LOGIN_ENABLED"] = "true"


@pytest.fixture(autouse=True)
def isolate_optional_azure_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", raising=False)
    monkeypatch.setenv("EMAIL_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AUTH_LOCAL_LOGIN_ENABLED", "true")
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
    monkeypatch.setenv("MANUAL_UPLOAD_DIR", str(tmp_path / "manual_uploads"))
    monkeypatch.setenv(
        "SHAREPOINT_INVOICE_CACHE_DIR", str(tmp_path / "sharepoint_cache")
    )
