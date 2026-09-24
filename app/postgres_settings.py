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
    connect_timeout: int = 15

    def __post_init__(self) -> None:
        allowed_ssl_modes = {"require", "verify-ca", "verify-full"}
        if self.sslmode == "disable" and self.host in LOCAL_POSTGRES_HOSTS:
            return
        if self.sslmode not in allowed_ssl_modes:
            raise ValueError(
                "PostgreSQL sslmode must be require, verify-ca, or verify-full. "
                "The disable mode is allowed only for a local loopback host."
            )
        if self.connect_timeout < 1:
            raise ValueError("PostgreSQL connect timeout must be at least one second.")

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
        try:
            connect_timeout = int(env.get("POSTGRES_CONNECT_TIMEOUT_SECONDS", "15"))
        except ValueError as error:
            raise ValueError("POSTGRES_CONNECT_TIMEOUT_SECONDS must be an integer.") from error
        return cls(
            host=env["AZURE_POSTGRES_HOST"],
            database=env["AZURE_POSTGRES_DATABASE"],
            user=env["AZURE_POSTGRES_USER"],
            password=env.get("AZURE_POSTGRES_PASSWORD") or None,
            port=port,
            sslmode=env.get("AZURE_POSTGRES_SSLMODE", "verify-full"),
            sslrootcert=env.get("AZURE_POSTGRES_SSLROOTCERT") or None,
            connect_timeout=connect_timeout,
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
            connect_timeout=int(query.get("connect_timeout", "15")),
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
            "connect_timeout": self.connect_timeout,
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
    pool_settings = (
        _pool_size("POSTGRES_POOL_MIN_SIZE", 1, allow_zero=True),
        _pool_size("POSTGRES_POOL_MAX_SIZE", 10),
        _pool_timeout(),
        _pool_max_idle(),
    )
    if resolved.password is not None:
        pool = _postgres_pool(resolved, *pool_settings)
    else:
        tenant_id = os.environ.get("OUTLOOK_MCP_TENANT_ID", "").strip()
        client_id = os.environ.get("OUTLOOK_MCP_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OUTLOOK_MCP_CLIENT_SECRET", "").strip()
        supplied = (tenant_id, client_id, client_secret)
        if any(supplied) and not all(supplied):
            raise ValueError(
                "The Invoice MCP tenant ID, client ID, and client secret "
                "must all be configured for PostgreSQL authentication."
            )
        pool = _entra_postgres_pool(
            resolved,
            tenant_id,
            client_id,
            client_secret,
            *pool_settings,
        )
    return pool.connection  # type: ignore[no-any-return]


@lru_cache(maxsize=4)
def _postgres_pool(
    settings: PostgresSettings,
    min_size: int,
    max_size: int,
    timeout: float,
    max_idle: float,
) -> object:
    if min_size > max_size:
        raise ValueError(
            "POSTGRES_POOL_MIN_SIZE must not exceed POSTGRES_POOL_MAX_SIZE."
        )
    pool_module = importlib.import_module("psycopg_pool")
    rows = importlib.import_module("psycopg.rows")
    return pool_module.ConnectionPool(
        kwargs={
            **settings.connection_kwargs(),
            "row_factory": rows.dict_row,
        },
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        max_idle=max_idle,
        check=pool_module.ConnectionPool.check_connection,
        open=True,
    )


@lru_cache(maxsize=4)
def _entra_postgres_pool(
    settings: PostgresSettings,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    min_size: int,
    max_size: int,
    timeout: float,
    max_idle: float,
) -> object:
    if min_size > max_size:
        raise ValueError(
            "POSTGRES_POOL_MIN_SIZE must not exceed POSTGRES_POOL_MAX_SIZE."
        )
    psycopg = importlib.import_module("psycopg")
    pool_module = importlib.import_module("psycopg_pool")
    rows = importlib.import_module("psycopg.rows")
    credential = _default_credential(tenant_id, client_id, client_secret)

    class EntraConnection(psycopg.Connection):  # type: ignore[name-defined, misc]
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: object) -> object:
            token = credential.get_token(AAD_SCOPE)  # type: ignore[attr-defined]
            return super().connect(conninfo, password=token.token, **kwargs)

    kwargs = settings.connection_kwargs(credential=_NoPasswordCredential())
    kwargs.pop("password", None)
    return pool_module.ConnectionPool(
        connection_class=EntraConnection,
        kwargs={
            **kwargs,
            "row_factory": rows.dict_row,
        },
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        max_idle=max_idle,
        max_lifetime=2700,
        check=pool_module.ConnectionPool.check_connection,
        open=True,
    )


class _NoPasswordCredential:
    def get_token(self, scope: str) -> object:
        del scope

        class EmptyToken:
            token = ""

        return EmptyToken()


def _pool_size(name: str, default: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    minimum = 0 if allow_zero else 1
    if value < minimum:
        requirement = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{name} must be {requirement}.")
    return value


def _pool_timeout() -> float:
    try:
        value = float(os.environ.get("POSTGRES_POOL_TIMEOUT_SECONDS", "30"))
    except ValueError as error:
        raise ValueError(
            "POSTGRES_POOL_TIMEOUT_SECONDS must be a number."
        ) from error
    if value <= 0:
        raise ValueError(
            "POSTGRES_POOL_TIMEOUT_SECONDS must be greater than zero."
        )
    return value


def _pool_max_idle() -> float:
    try:
        value = float(os.environ.get("POSTGRES_POOL_MAX_IDLE_SECONDS", "300"))
    except ValueError as error:
        raise ValueError(
            "POSTGRES_POOL_MAX_IDLE_SECONDS must be a number."
        ) from error
    if value <= 0:
        raise ValueError(
            "POSTGRES_POOL_MAX_IDLE_SECONDS must be greater than zero."
        )
    return value
