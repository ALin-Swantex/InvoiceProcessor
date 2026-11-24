import os

import pytest


# Isolate the whole suite from the developer's real configuration. The flag
# stops load_project_environment() reading .env, and any credentials or
# behaviour settings already exported in the shell (for example a sourced
# .env with AUTH_COOKIE_SECURE=true) are removed so tests see the defaults and
# never reach live services.
os.environ["INVOICE_PROCESSOR_SKIP_DOTENV"] = "1"
# Behaviour settings the app reads; cleared again before every test so one
# test's direct os.environ write cannot leak into the next.
BEHAVIOUR_VARIABLES = (
    "AI_CONFIDENCE_THRESHOLD",
    "APP_PUBLIC_URL",
    "APPROVAL_REMINDER_SCAN_SECONDS",
    "AUTH_COOKIE_SECURE",
    "INVOICE_STORE_BACKEND",
    "LOG_LEVEL",
    "OUTLOOK_LOCAL_POLLING_ENABLED",
    "OUTLOOK_LOCAL_POLLING_LIMIT",
    "OUTLOOK_LOCAL_POLLING_LOOKBACK_DAYS",
    "OUTLOOK_PROCESSING_STALE_SECONDS",
    "OUTLOOK_WORKER_CONCURRENCY",
    "OUTLOOK_WORKER_POLL_SECONDS",
    "PURCHASE_LEDGER_NOTIFICATION_EMAIL",
    "WORKER_STALE_AFTER_SECONDS",
)
for _name in list(os.environ):
    if _name.startswith(
        (
            "GRAPH_",
            "OUTLOOK_",
            "AZURE_POSTGRES_",
            "POSTGRES_",
            "SHAREPOINT_",
            "EXCEL_CONVERSION_",
            "AUTH_",
            "EMAIL_",
            "SAGE_",
        )
    ) or _name.startswith(("ANTHROPIC_", "EXTRACTION_", "OPENAI_")) or _name in {
        "DATABASE_URL",
        "CONFIG_DB_PATH",
        "CONFIG_STORE_BACKEND",
        "INVOICE_DB_PATH",
        "INVOICE_EXTRACTION_MODEL",
        "INVOICE_EXTRACTION_PROMPT_VERSION",
        "MANUAL_UPLOAD_DIR",
        *BEHAVIOUR_VARIABLES,
    }:
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
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "EXTRACTION_PROVIDER",
        *BEHAVIOUR_VARIABLES,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMAIL_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AUTH_LOCAL_LOGIN_ENABLED", "true")
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
    monkeypatch.setenv("INVOICE_DB_PATH", str(tmp_path / "invoices.db"))
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
