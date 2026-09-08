from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping
from urllib.parse import parse_qsl, unquote, urlparse


AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
LOCAL_POSTGRES_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ConnectionFactory = Callable[[], object]


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    database: str
    user: str
    password: str | None = None
    port: int = 5432
    sslmode: str = "verify-full"
    sslrootcert: str | None = None

    def __post_init__(self) -> None:
        allowed_ssl_modes = {"require", "verify-ca", "verify-full"}
        if self.sslmode == "disable" and self.host in LOCAL_POSTGRES_HOSTS:
            return
        if self.sslmode not in allowed_ssl_modes:
            raise ValueError(
                "PostgreSQL sslmode must be require, verify-ca, or verify-full. "
                "The disable mode is allowed only for a local loopback host."
            )

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> PostgresSettings:
        env = os.environ if environ is None else environ
        database_url = env.get("DATABASE_URL")
        if database_url:
            return cls.from_database_url(database_url)

        required = ("AZURE_POSTGRES_HOST", "AZURE_POSTGRES_DATABASE", "AZURE_POSTGRES_USER")
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise ValueError(
                "PostgreSQL configuration is missing: " + ", ".join(missing)
            )
        try:
            port = int(env.get("AZURE_POSTGRES_PORT", "5432"))
        except ValueError as error:
            raise ValueError("AZURE_POSTGRES_PORT must be an integer.") from error
        return cls(
            host=env["AZURE_POSTGRES_HOST"],
            database=env["AZURE_POSTGRES_DATABASE"],
            user=env["AZURE_POSTGRES_USER"],
            password=env.get("AZURE_POSTGRES_PASSWORD") or None,
            port=port,
            sslmode=env.get("AZURE_POSTGRES_SSLMODE", "verify-full"),
            sslrootcert=env.get("AZURE_POSTGRES_SSLROOTCERT") or None,
        )

    @classmethod
    def from_database_url(cls, value: str) -> PostgresSettings:
        parsed = urlparse(value)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("DATABASE_URL must use the postgres or postgresql scheme.")
        if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
            raise ValueError("DATABASE_URL must include host, user, and database.")
        query = dict(parse_qsl(parsed.query))
        try:
            port = parsed.port or 5432
        except ValueError as error:
            raise ValueError("DATABASE_URL contains an invalid port.") from error
        return cls(
            host=parsed.hostname,
            port=port,
            database=unquote(parsed.path.lstrip("/")),
            user=unquote(parsed.username),
            password=unquote(parsed.password) if parsed.password else None,
            sslmode=query.get("sslmode", "verify-full"),
            sslrootcert=query.get("sslrootcert"),
        )

    def connection_kwargs(self, credential: object | None = None) -> dict[str, object]:
        password = self.password
        if password is None:
            if credential is None:
                tenant_id = os.environ.get("OUTLOOK_MCP_TENANT_ID", "").strip()
                client_id = os.environ.get("OUTLOOK_MCP_CLIENT_ID", "").strip()
                client_secret = os.environ.get(
                    "OUTLOOK_MCP_CLIENT_SECRET", ""
                ).strip()
                supplied = (tenant_id, client_id, client_secret)
                if any(supplied) and not all(supplied):
                    raise ValueError(
                        "The Invoice MCP tenant ID, client ID, and client secret "
                        "must all be configured for PostgreSQL authentication."
                    )
                credential = _default_credential(
                    tenant_id, client_id, client_secret
                )
            password = credential.get_token(AAD_SCOPE).token  # type: ignore[attr-defined]
        kwargs: dict[str, object] = {
            "host": self.host,
            "dbname": self.database,
            "user": self.user,
            "password": password,
            "port": self.port,
            "sslmode": self.sslmode,
        }
        if self.sslmode in {"verify-ca", "verify-full"}:
            if self.sslrootcert:
                kwargs["sslrootcert"] = self.sslrootcert
            else:
                certifi = importlib.import_module("certifi")
                kwargs["sslrootcert"] = certifi.where()
        return kwargs


@lru_cache(maxsize=4)
def _default_credential(
    tenant_id: str, client_id: str, client_secret: str
) -> object:
    identity = importlib.import_module("azure.identity")
    if tenant_id and client_id and client_secret:
        return identity.ClientSecretCredential(tenant_id, client_id, client_secret)
    return identity.DefaultAzureCredential()


def connect_postgres(
    settings: PostgresSettings,
    *,
    credential: object | None = None,
) -> object:
    """Connect lazily so SQLite-only installations need no optional packages."""
    psycopg = importlib.import_module("psycopg")
    rows = importlib.import_module("psycopg.rows")
    return psycopg.connect(
        **settings.connection_kwargs(credential),
        row_factory=rows.dict_row,
    )


def postgres_connection_factory(
    settings: PostgresSettings | None = None,
) -> ConnectionFactory:
    resolved = settings or PostgresSettings.from_env()
    return lambda: connect_postgres(resolved)
