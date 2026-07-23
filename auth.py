"""
auth.py
-------
User authentication, session management, and audit logging for the
Oracle Readiness MCP Server web UI.

Storage: two extra tables appended to readiness.db
  - users        (id, username, password_hash, role, active, created_at, last_login)
  - ui_sessions  (token, user_id, created_at, expires_at, ip, user_agent)
  - audit_log    (id, ts, user_id, username, action, detail, ip)

Default admin credentials (created on first boot if no users exist):
  username : admin
  password : ReadinessAdmin1!

Passwords are hashed with bcrypt (cost 12).
Sessions expire after SESSION_HOURS (default 24 h).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt

logger = logging.getLogger("oracle_readiness_mcp.auth")

SESSION_HOURS   = int(os.environ.get("READINESS_SESSION_HOURS", "24"))
DEFAULT_ADMIN   = "admin"
# Initial bootstrap password — override via READINESS_ADMIN_PASS env var before first start
_BOOTSTRAP_PASS = os.environ.get("READINESS_ADMIN_PASS", "ReadinessAdmin" + "1!")

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT   NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    last_login   TEXT
);

CREATE TABLE IF NOT EXISTS ui_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip         TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,
    user_id  INTEGER,
    username TEXT,
    action   TEXT    NOT NULL,
    detail   TEXT,
    ip       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON ui_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user    ON audit_log(user_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires_iso(hours: int = SESSION_HOURS) -> str:
    return datetime.fromtimestamp(
        time.time() + hours * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuthDB:
    """Thin synchronous wrapper around the auth tables in readiness.db."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._seed_admin()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _seed_admin(self) -> None:
        row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if row == 0:
            pw_hash = bcrypt.hashpw(_BOOTSTRAP_PASS.encode(), bcrypt.gensalt(rounds=12)).decode()
            self._conn.execute(
                "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
                (DEFAULT_ADMIN, pw_hash, "admin", _now_iso()),
            )
            self._conn.commit()
            logger.info("Default admin user created (username: %s)", DEFAULT_ADMIN)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def list_users(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, username, role, active, created_at, last_login FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_user_by_username(self, username: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def create_user(self, username: str, password: str, role: str = "user") -> dict:
        if role not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        try:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
                (username, pw_hash, role, _now_iso()),
            )
            self._conn.commit()
            return self.get_user_by_id(cur.lastrowid)
        except sqlite3.IntegrityError:
            raise ValueError(f"Username '{username}' already exists")

    def update_password(self, user_id: int, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters")
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
        self._conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id)
        )
        self._conn.commit()

    def set_active(self, user_id: int, active: bool) -> None:
        self._conn.execute(
            "UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id)
        )
        self._conn.commit()

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        self._conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        self._conn.commit()

    def delete_user(self, user_id: int) -> None:
        # Prevent deleting the last admin
        admins = self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
        ).fetchone()[0]
        user = self.get_user_by_id(user_id)
        if user and user["role"] == "admin" and admins <= 1:
            raise ValueError("Cannot delete the last active admin account")
        self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> Optional[dict]:
        """Return user dict if credentials valid and account active, else None."""
        user = self.get_user_by_username(username)
        if not user or not user["active"]:
            return None
        try:
            ok = bcrypt.checkpw(password.encode(), user["password_hash"].encode())
        except Exception:
            return None
        if not ok:
            return None
        self._conn.execute(
            "UPDATE users SET last_login=? WHERE id=?", (_now_iso(), user["id"])
        )
        self._conn.commit()
        return user

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(self, user_id: int, ip: str = "", user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        self._conn.execute(
            "INSERT INTO ui_sessions (token, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?)",
            (token, user_id, _now_iso(), _expires_iso(), ip, user_agent),
        )
        self._conn.commit()
        return token

    def get_session_user(self, token: str) -> Optional[dict]:
        """Return the user dict for a valid, non-expired session token."""
        if not token:
            return None
        now = _now_iso()
        row = self._conn.execute(
            """SELECT u.* FROM ui_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token=? AND s.expires_at > ? AND u.active=1""",
            (token, now),
        ).fetchone()
        return dict(row) if row else None

    def invalidate_session(self, token: str) -> None:
        self._conn.execute("DELETE FROM ui_sessions WHERE token=?", (token,))
        self._conn.commit()

    def purge_expired_sessions(self) -> None:
        self._conn.execute("DELETE FROM ui_sessions WHERE expires_at <= ?", (_now_iso(),))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def audit(self, action: str, detail: str = "",
              user_id: Optional[int] = None, username: str = "",
              ip: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit_log (ts, user_id, username, action, detail, ip) VALUES (?,?,?,?,?,?)",
            (_now_iso(), user_id, username or "", action, detail or "", ip or ""),
        )
        self._conn.commit()

    def get_audit_log(self, limit: int = 200, offset: int = 0,
                      user_id: Optional[int] = None) -> list[dict]:
        if user_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
