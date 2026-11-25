from pathlib import Path
import logging
import os

from dotenv import load_dotenv


# Variables that were renamed. The new name is canonical; a value still set under
# the old name is used when the new name is missing, so existing .env files,
# secret stores and container settings keep working while they are migrated.
RENAMED_ENVIRONMENT_VARIABLES: dict[str, str] = {
    "GRAPH_TENANT_ID": "OUTLOOK_MCP_TENANT_ID",
    "GRAPH_CLIENT_ID": "OUTLOOK_MCP_CLIENT_ID",
    "GRAPH_CLIENT_SECRET": "OUTLOOK_MCP_CLIENT_SECRET",
    "GRAPH_MAILBOX": "OUTLOOK_MCP_MAILBOX",
    "OUTLOOK_DOWNLOAD_DIR": "OUTLOOK_MCP_DOWNLOAD_DIR",
    "OUTLOOK_MAX_PDF_BYTES": "OUTLOOK_MCP_MAX_PDF_BYTES",
    "OUTLOOK_MAX_EXCEL_BYTES": "OUTLOOK_MCP_MAX_EXCEL_BYTES",
}


def apply_renamed_environment_variables() -> None:
    """Fill each renamed variable from its legacy name when only that is set."""
    for new_name, old_name in RENAMED_ENVIRONMENT_VARIABLES.items():
        if os.environ.get(new_name, "").strip():
            continue
        legacy_value = os.environ.get(old_name, "")
        if legacy_value.strip():
            os.environ[new_name] = legacy_value
            logging.getLogger("invoice_processor").warning(
                "Environment variable %s is deprecated; rename it to %s.",
                old_name,
                new_name,
            )


def load_project_environment() -> None:
    # Tests (and tools that supply their own environment) set this flag so the
    # developer's real .env can never leak into an isolated run.
    if os.environ.get("INVOICE_PROCESSOR_SKIP_DOTENV") != "1":
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    apply_renamed_environment_variables()


def project_path_from_environment(name: str, default: str) -> Path:
    """Resolve application data paths consistently, regardless of the cwd."""
    path = Path(os.environ.get(name, default))
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number.") from error


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number.") from error
