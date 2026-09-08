import pytest


@pytest.fixture(autouse=True)
def isolate_optional_azure_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", raising=False)
