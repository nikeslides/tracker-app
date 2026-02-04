"""
SQLite-backed account system for the player.
Used when the server is started with --accounts.
"""

import sqlite3
import secrets
from pathlib import Path
from typing import List, Optional
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT,
    used_by_user_id INTEGER,
    FOREIGN KEY (used_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_lastfm (
    user_id INTEGER PRIMARY KEY,
    session_key TEXT NOT NULL,
    lastfm_username TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_favorites (
    user_id INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_invite_key ON invite_keys(key);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_user_favorites_user ON user_favorites(user_id);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Create database and tables if they don't exist."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


def get_user_by_username(db_path: Path, username: str) -> Optional[sqlite3.Row]:
    """Return user row if exists, else None."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
            (username.strip(),),
        )
        return cur.fetchone()


def get_user_by_id(db_path: Path, user_id: int) -> Optional[sqlite3.Row]:
    """Return user row if exists, else None."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        return cur.fetchone()


def verify_password(user_row: sqlite3.Row, password: str) -> bool:
    """Check password against stored hash."""
    return bool(check_password_hash(user_row["password_hash"], password))


def create_user(db_path: Path, username: str, password: str) -> Optional[sqlite3.Row]:
    """Create a new user. Returns user row or None if username taken."""
    username = username.strip()
    if not username or not password:
        return None
    password_hash = generate_password_hash(password)
    created_at = datetime.utcnow().isoformat() + "Z"
    with get_connection(db_path) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, created_at),
            )
            conn.commit()
            return get_user_by_id(db_path, cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def create_invite_key(db_path: Path) -> str:
    """Create a new invite key and return it."""
    key = secrets.token_urlsafe(24)
    created_at = datetime.utcnow().isoformat() + "Z"
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO invite_keys (key, created_at) VALUES (?, ?)",
            (key, created_at),
        )
        conn.commit()
    return key


def validate_invite_key(db_path: Path, key: str) -> bool:
    """Return True if key exists and has not been used."""
    key = key.strip()
    if not key:
        return False
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT id FROM invite_keys WHERE key = ? AND used_at IS NULL",
            (key,),
        )
        return cur.fetchone() is not None


def use_invite_key(db_path: Path, key: str, user_id: int) -> bool:
    """Mark invite key as used by user_id. Returns True if key was valid and updated."""
    key = key.strip()
    if not key:
        return False
    used_at = datetime.utcnow().isoformat() + "Z"
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE invite_keys SET used_at = ?, used_by_user_id = ? WHERE key = ? AND used_at IS NULL",
            (used_at, user_id, key),
        )
        conn.commit()
        return cur.rowcount > 0


# --- Last.fm (per-account) ---

def get_lastfm_session(db_path: Path, user_id: int) -> Optional[sqlite3.Row]:
    """Return user_lastfm row for user_id, or None."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT user_id, session_key, lastfm_username, updated_at FROM user_lastfm WHERE user_id = ?",
            (user_id,),
        )
        return cur.fetchone()


def set_lastfm_session(db_path: Path, user_id: int, session_key: str, lastfm_username: str) -> None:
    """Store or update Last.fm session for user."""
    updated_at = datetime.utcnow().isoformat() + "Z"
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO user_lastfm (user_id, session_key, lastfm_username, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 session_key = excluded.session_key,
                 lastfm_username = excluded.lastfm_username,
                 updated_at = excluded.updated_at""",
            (user_id, session_key, lastfm_username.strip(), updated_at),
        )
        conn.commit()


def clear_lastfm_session(db_path: Path, user_id: int) -> bool:
    """Remove Last.fm link for user. Returns True if a row was deleted."""
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM user_lastfm WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0


# --- Favorites (per-account) ---

def get_favorites(db_path: Path, user_id: int) -> List[str]:
    """Return list of track_ids favorited by user."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "SELECT track_id FROM user_favorites WHERE user_id = ? ORDER BY track_id",
            (user_id,),
        )
        return [row["track_id"] for row in cur.fetchall()]


def add_favorite(db_path: Path, user_id: int, track_id: str) -> None:
    """Add track to user's favorites. Idempotent."""
    track_id = (track_id or "").strip()
    if not track_id:
        return
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_favorites (user_id, track_id) VALUES (?, ?)",
            (user_id, track_id),
        )
        conn.commit()


def remove_favorite(db_path: Path, user_id: int, track_id: str) -> bool:
    """Remove track from user's favorites. Returns True if a row was deleted."""
    track_id = (track_id or "").strip()
    if not track_id:
        return False
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND track_id = ?",
            (user_id, track_id),
        )
        conn.commit()
        return cur.rowcount > 0
