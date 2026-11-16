import os

import pytest


# Isolate the whole suite from the developer's real configuration. The flag
# stops load_project_environment() reading .env, and any credentials already
# exported in the shell are removed so tests never reach live services.
os.environ["INVOICE_PROCESSOR_SKIP_DOTENV"] = "1"
for _name in list(os.environ):
    if _name.startswith(
        ("OUTLOOK_MCP_", "AZURE_POSTGRES_", "SHAREPOINT_", "EXCEL_CONVERSION_")
    ) or _name in {"DATABASE_URL", "INVOICE_EXTRACTION_MODEL", "AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID"}:
        del os.environ[_name]

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


@pytest.fixture(autouse=True)
def no_real_graph_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Graph retries back off with real sleeps in production; never in tests."""
    slept: list[float] = []
    monkeypatch.setattr("app.graph_http.sleep_function", slept.append)
    return slept
