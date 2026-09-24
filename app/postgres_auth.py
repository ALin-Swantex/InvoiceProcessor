from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from app.auth import ALL_ROLES, SEED_USERS, SESSION_LIFETIME, AuthError, User
from app.postgres_settings import ConnectionFactory, PostgresSettings, postgres_connection_factory


class PostgresAuthStore:
    """PostgreSQL sessions and OAuth state for the web application."""

    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._connection_factory = connection_factory or postgres_connection_factory(settings)

    def authenticate(self, username: str, password: str) -> User:
        user = next(
            (
                User(seed_username, display_name, email, role)
                for seed_username, display_name, email, role, seed_password in SEED_USERS
                if secrets.compare_digest(seed_username.encode(), username.encode())
                and secrets.compare_digest(seed_password.encode(), password.encode())
            ),
            None,
        )
        if user is None:
            raise AuthError("Invalid username or password.")
        return user

    def create_session(self, username: str) -> str:
        user = next(
            (
                User(seed_username, display_name, email, role)
                for seed_username, display_name, email, role, _ in SEED_USERS
                if seed_username == username
            ),
            None,
        )
        if user is None:
            raise AuthError(f"Unknown local development identity '{username}'.")
        return self.create_federated_session(user)

    def create_federated_session(self, user: User) -> str:
        if user.role not in ALL_ROLES:
            raise AuthError(f"Unknown role '{user.role}'.")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_sessions (
                        token_hash, username, display_name, email, role,
                        created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self._token_hash(token),
                        user.username,
                        user.display_name,
                        user.email,
                        user.role,
                        now,
                        now + SESSION_LIFETIME,
                    ),
                )
        return token

    def save_oauth_flow(self, state: str, flow: dict[str, object]) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO oauth_flows (state, flow_json, expires_at)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (state) DO UPDATE
                    SET flow_json = excluded.flow_json,
                        expires_at = excluded.expires_at
                    """,
                    (state, json.dumps(flow), expires_at),
                )

    def pop_oauth_flow(self, state: str) -> dict[str, object] | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM oauth_flows WHERE state = %s "
                    "RETURNING flow_json, expires_at",
                    (state,),
                )
                row = cursor.fetchone()
        if row is None or row["expires_at"] < datetime.now(timezone.utc):
            return None
        flow = row["flow_json"]
        if isinstance(flow, str):
            flow = json.loads(flow)
        return flow if isinstance(flow, dict) else None

    def get_user_by_session(self, token: str) -> User | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT username, display_name, email, role, expires_at
                    FROM auth_sessions
                    WHERE token_hash = %s
                    """,
                    (self._token_hash(token),),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        if row["expires_at"] < datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return User(
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            email=str(row["email"]) if row["email"] is not None else None,
            role=str(row["role"]),
        )

    def delete_session(self, token: str) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = %s",
                    (self._token_hash(token),),
                )

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

