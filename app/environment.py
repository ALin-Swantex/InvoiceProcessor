from pathlib import Path
import os

from dotenv import load_dotenv


def load_project_environment() -> None:
    # Tests (and tools that supply their own environment) set this flag so the
    # developer's real .env can never leak into an isolated run.
    if os.environ.get("INVOICE_PROCESSOR_SKIP_DOTENV") == "1":
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def project_path_from_environment(name: str, default: str) -> Path:
    """Resolve application data paths consistently, regardless of the cwd."""
    path = Path(os.environ.get(name, default))
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[1] / path
