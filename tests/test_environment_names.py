import pytest

from app.environment import (
    RENAMED_ENVIRONMENT_VARIABLES,
    apply_renamed_environment_variables,
)


def test_legacy_names_still_work_when_the_new_name_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for new_name, old_name in RENAMED_ENVIRONMENT_VARIABLES.items():
        # setenv first so monkeypatch records the variable and removes the
        # value apply_renamed_environment_variables() writes; delenv alone
        # records nothing for an absent variable, and the fake values would
        # leak into every later test.
        monkeypatch.setenv(new_name, "")
        monkeypatch.delenv(new_name)
        monkeypatch.setenv(old_name, f"legacy-{new_name}")

    apply_renamed_environment_variables()

    import os

    for new_name in RENAMED_ENVIRONMENT_VARIABLES:
        assert os.environ[new_name] == f"legacy-{new_name}"


def test_new_name_wins_over_the_legacy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPH_MAILBOX", "new@example.test")
    monkeypatch.setenv("OUTLOOK_MCP_MAILBOX", "old@example.test")

    apply_renamed_environment_variables()

    import os

    assert os.environ["GRAPH_MAILBOX"] == "new@example.test"


def test_blank_legacy_value_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
    monkeypatch.setenv("OUTLOOK_MCP_TENANT_ID", "   ")

    apply_renamed_environment_variables()

    import os

    assert "GRAPH_TENANT_ID" not in os.environ
