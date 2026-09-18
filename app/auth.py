from __future__ import annotations

import hashlib
import hmac
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
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS federated_sessions (
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

# Local users remain available for development and automated tests. Production
# deployments should configure Microsoft Entra login and leave
# AUTH_LOCAL_LOGIN_ENABLED=false. Entra sessions use the same role guards but
# do not create local password-bearing user records.

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
    """SQLite-backed sessions plus development-only local users."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()
        self._seed_default_users()

    def _seed_default_users(self) -> None:
        with self._connect() as connection:
            existing = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if existing["n"]:
                return
        for username, display_name, email, role, password in SEED_USERS:
            self.create_user(
                username=username,
                display_name=display_name,
                email=email,
                role=role,
                password=password,
            )

    # ------------------------------------------------------------------
    # User management (admin-only via the API layer)
    # ------------------------------------------------------------------

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        email: str | None,
        role: str,
        password: str,
    ) -> User:
        if role not in ALL_ROLES:
            raise AuthError(f"Unknown role '{role}'.")
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        username, display_name, email, role,
                        password_hash, password_salt, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, display_name, email, role, password_hash, salt, created_at),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise AuthError(f"User '{username}' already exists.") from error
        return User(username=username, display_name=display_name, email=email, role=role)

    def list_users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, display_name, email, role FROM users ORDER BY role, username"
            ).fetchall()
        return [User(**dict(row)) for row in rows]

    def update_user(self, username: str, **fields: object) -> User:
        allowed = {"display_name", "email", "role", "password"}
        unknown = set(fields) - allowed
        if unknown:
            raise AuthError(f"Unknown user fields: {', '.join(sorted(unknown))}.")
        role = fields.get("role")
        if role is not None and role not in ALL_ROLES:
            raise AuthError(f"Unknown role '{role}'.")
        password = fields.pop("password", None)
        if password is not None:
            if not isinstance(password, str) or not password:
                raise AuthError("A new password cannot be empty.")
            salt = secrets.token_hex(16)
            fields["password_hash"] = _hash_password(password, salt)
            fields["password_salt"] = salt
        if not fields:
            users = {user.username: user for user in self.list_users()}
            if username not in users:
                raise AuthError(f"User '{username}' was not found.")
            return users[username]
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    f"UPDATE users SET {assignments} WHERE username = ?",
                    (*fields.values(), username),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise AuthError(
                    "Another user already has that email address."
                ) from error
            if cursor.rowcount == 0:
                raise AuthError(f"User '{username}' was not found.")
        return next(user for user in self.list_users() if user.username == username)

    def delete_user(self, username: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE username = ?", (username,))
            connection.commit()
            if cursor.rowcount == 0:
                raise AuthError(f"User '{username}' was not found.")

    def set_password(self, username: str, password: str) -> None:
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ? WHERE username = ?",
                (password_hash, salt, username),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise AuthError(f"User '{username}' was not found.")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT username, display_name, email, role, password_hash, password_salt
                FROM users WHERE username = ?
                """,
                (username,),
            ).fetchone()
        if row is None:
            raise AuthError("Invalid username or password.")
        expected = _hash_password(password, row["password_salt"])
        if not hmac.compare_digest(expected, row["password_hash"]):
            raise AuthError("Invalid username or password.")
        return User(
            username=row["username"],
            display_name=row["display_name"],
            email=row["email"],
            role=row["role"],
        )

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + SESSION_LIFETIME
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, username, now.isoformat(), expires_at.isoformat()),
            )
            connection.commit()
        return token

    def create_federated_session(self, user: User) -> str:
        if user.role not in ALL_ROLES:
            raise AuthError(f"Unknown role '{user.role}'.")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + SESSION_LIFETIME
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO federated_sessions (
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
                SELECT users.username AS username, users.display_name AS display_name,
                       users.email AS email, users.role AS role,
                       sessions.expires_at AS expires_at
                FROM sessions
                JOIN users ON users.username = sessions.username
                WHERE sessions.token = ?
                """,
                (token,),
            ).fetchone()
        if row is None:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT username, display_name, email, role, expires_at
                    FROM federated_sessions
                    WHERE token = ?
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
            connection.execute(
                "DELETE FROM federated_sessions WHERE token = ?", (token,)
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _hash_password(password: str, salt: str) -> str:
    # PBKDF2-HMAC-SHA256 is used only by the optional local-development login.
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()


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
