from __future__ import annotations

import os
import secrets
import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request


# ---------------------------------------------------------------------------
# ROLE MODEL
# ---------------------------------------------------------------------------
# These are the only roles the system understands. They map directly onto
# MANUAL_VS_AUTOMATED.md: every endpoint and every frontend section is
# restricted to the role(s) that should be able to see or act on it.
ROLE_ADMIN = "admin"
ROLE_PURCHASE_LEDGER = "purchase_ledger"
ROLE_APPROVER_1 = "approver1"
ROLE_APPROVER_2 = "approver2"
ROLE_PURCHASING = "purchasing"

ALL_ROLES = (
    ROLE_ADMIN,
    ROLE_PURCHASE_LEDGER,
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    ROLE_PURCHASING,
)

SESSION_COOKIE_NAME = "session_token"
SESSION_LIFETIME = timedelta(hours=12)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_flows (
    state TEXT PRIMARY KEY,
    flow_json TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# These identities support optional local development and automated tests only.
# They are never persisted. Production uses Microsoft Entra app roles.

SEED_USERS: list[tuple[str, str, str, str, str]] = [
    # username, display name, email, role, placeholder password
    ("admin", "System Administrator", "admin@example.test", ROLE_ADMIN, "ChangeMe-Admin1!"),
    (
        "purchase.ledger",
        "Purchase Ledger",
        "purchase-ledger@example.test",
        ROLE_PURCHASE_LEDGER,
        "ChangeMe-PL1!",
    ),
    (
        "jordan.blake",
        "Jordan Blake (Approver 1)",
        "jordan.blake@example.test",
        ROLE_APPROVER_1,
        "ChangeMe-App1!",
    ),
    (
        "sam.ellis",
        "Sam Ellis (Approver 2)",
        "sam.ellis@example.test",
        ROLE_APPROVER_2,
        "ChangeMe-App2!",
    ),
    (
        "purchasing.team",
        "Purchasing",
        "purchasing@example.test",
        ROLE_PURCHASING,
        "ChangeMe-Pur1!",
    ),
]


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    email: str | None
    role: str


class AuthError(ValueError):
    pass


class AuthStore:
    """SQLite-backed sessions for identities supplied by Microsoft Entra."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._migrate_legacy_identity_tables(connection)
            connection.executescript(SCHEMA)
            connection.commit()

    @staticmethod
    def _migrate_legacy_identity_tables(connection: sqlite3.Connection) -> None:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        session_columns = (
            {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
            if "sessions" in tables
            else set()
        )
        legacy_tables = {"users", "federated_sessions"} & tables
        if not legacy_tables and (
            not session_columns or "display_name" in session_columns
        ):
            return

        connection.execute(
            """
            CREATE TABLE sessions_replacement (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                display_name TEXT NOT NULL,
                email TEXT,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        if "sessions" in tables and "display_name" in session_columns:
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions_replacement
                SELECT token, username, display_name, email, role,
                       created_at, expires_at
                FROM sessions
                """
            )
        elif "sessions" in tables and "users" in tables:
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions_replacement
                SELECT sessions.token, users.username, users.display_name,
                       users.email, users.role, sessions.created_at,
                       sessions.expires_at
                FROM sessions
                JOIN users ON users.username = sessions.username
                """
            )
        if "federated_sessions" in tables:
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions_replacement
                SELECT token, username, display_name, email, role,
                       created_at, expires_at
                FROM federated_sessions
                """
            )
        connection.executescript(
            """
            DROP TABLE IF EXISTS sessions;
            ALTER TABLE sessions_replacement RENAME TO sessions;
            DROP TABLE IF EXISTS federated_sessions;
            DROP TABLE IF EXISTS users;
            """
        )

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User:
        user = next(
            (
                User(seed_username, display_name, email, role)
                for seed_username, display_name, email, role, seed_password
                in SEED_USERS
                if secrets.compare_digest(
                    seed_username.encode(), username.encode()
                )
                and secrets.compare_digest(
                    seed_password.encode(), password.encode()
                )
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
        expires_at = now + SESSION_LIFETIME
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    token, username, display_name, email, role,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    user.username,
                    user.display_name,
                    user.email,
                    user.role,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            connection.commit()
        return token

    def save_oauth_flow(self, state: str, flow: dict[str, object]) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO oauth_flows (
                    state, flow_json, expires_at
                ) VALUES (?, ?, ?)
                """,
                (state, json.dumps(flow), expires_at.isoformat()),
            )
            connection.commit()

    def pop_oauth_flow(self, state: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT flow_json, expires_at FROM oauth_flows WHERE state = ?",
                (state,),
            ).fetchone()
            connection.execute(
                "DELETE FROM oauth_flows WHERE state = ?", (state,)
            )
            connection.commit()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            return None
        flow = json.loads(row["flow_json"])
        return flow if isinstance(flow, dict) else None

    def get_user_by_session(self, token: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT username, display_name, email, role, expires_at
                FROM sessions
                WHERE sessions.token = ?
                """,
                (token,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return User(
            username=row["username"],
            display_name=row["display_name"],
            email=row["email"],
            role=row["role"],
        )

    def delete_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> User:
    auth_store: AuthStore = request.app.state.auth_store
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in.")
    user = auth_store.get_user_by_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session has expired. Please sign in again.")
    return user


def require_role(*roles: str):
    """FastAPI dependency factory restricting an endpoint to one or more
    roles. Use as: `user: User = Depends(require_role(ROLE_ADMIN))`."""

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Role '{user.role}' is not permitted to access this resource. "
                    f"Requires one of: {', '.join(roles)}."
                ),
            )
        return user

    return _dependency


def auth_store_from_environment() -> AuthStore:
    return AuthStore(Path(os.environ.get("AUTH_DB_PATH", "runtime_data/auth.db")))
