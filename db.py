import sqlite3
import json
from typing import Optional, Any, Dict, List, Tuple
from datetime import datetime, timezone

DB_PATH = "bot.db"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_vip INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 0,
            notify_new_prompts INTEGER DEFAULT 1,
            referrals_count INTEGER DEFAULT 0,
            state TEXT,
            state_payload TEXT,
            created_at TEXT,
            last_seen TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            prompt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            tags TEXT,
            source TEXT,
            source_chat_id TEXT,
            source_post_id TEXT,
            created_by INTEGER,
            created_at TEXT,
            is_new INTEGER DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            prompt_id INTEGER NOT NULL,
            created_at TEXT,
            PRIMARY KEY (user_id, prompt_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL,
            created_at TEXT,
            PRIMARY KEY (referrer_id, referred_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS freepik_tasks (
            task_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT
        )
        """)
        conn.commit()


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            conn.execute("""
                UPDATE users SET username=?, first_name=?, last_seen=?
                WHERE user_id=?
            """, (username, first_name, _utcnow(), user_id))
        else:
            conn.execute("""
                INSERT INTO users(user_id, username, first_name, created_at, last_seen)
                VALUES(?,?,?,?,?)
            """, (user_id, username, first_name, _utcnow(), _utcnow()))
        conn.commit()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def set_state(user_id: int, state: Optional[str], payload: Optional[Dict[str, Any]] = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users SET state=?, state_payload=?, last_seen=?
            WHERE user_id=?
        """, (state, json.dumps(payload) if payload else None, _utcnow(), user_id))
        conn.commit()


def get_state(user_id: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    u = get_user(user_id)
    if not u:
        return None, None
    state = u.get("state")
    payload_raw = u.get("state_payload")
    payload = json.loads(payload_raw) if payload_raw else None
    return state, payload


def set_vip(user_id: int, is_vip: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_vip=?, last_seen=? WHERE user_id=?",
                     (1 if is_vip else 0, _utcnow(), user_id))
        conn.commit()


def toggle_notify(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT notify_new_prompts FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur = int(row[0]) if row else 1
        newv = 0 if cur == 1 else 1
        conn.execute("UPDATE users SET notify_new_prompts=?, last_seen=? WHERE user_id=?",
                     (newv, _utcnow(), user_id))
        conn.commit()
        return newv


def list_notified_users() -> List[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE notify_new_prompts=1").fetchall()
        return [int(r[0]) for r in rows]


def add_prompt(
    text: str,
    tags: str | None = None,
    source: str | None = None,
    source_chat_id: str | None = None,
    source_post_id: str | None = None,
    created_by: int | None = None
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO prompts(text, tags, source, source_chat_id, source_post_id, created_by, created_at, is_new)
            VALUES(?,?,?,?,?,?,?,1)
        """, (text, tags, source, source_chat_id, source_post_id, created_by, _utcnow()))
        conn.commit()
        return int(cur.lastrowid)


def list_prompts(limit: int = 10, only_new: bool = False) -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if only_new:
            rows = conn.execute("""
                SELECT * FROM prompts WHERE is_new=1 ORDER BY prompt_id DESC LIMIT ?
            """, (limit,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM prompts ORDER BY prompt_id DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def mark_prompt_seen(prompt_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE prompts SET is_new=0 WHERE prompt_id=?", (prompt_id,))
        conn.commit()


def toggle_favorite(user_id: int, prompt_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND prompt_id=?", (user_id, prompt_id)).fetchone()
        if row:
            conn.execute("DELETE FROM favorites WHERE user_id=? AND prompt_id=?", (user_id, prompt_id))
            conn.commit()
            return False
        conn.execute("INSERT INTO favorites(user_id, prompt_id, created_at) VALUES(?,?,?)",
                     (user_id, prompt_id, _utcnow()))
        conn.commit()
        return True


def add_referral(referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM referrals WHERE referrer_id=? AND referred_id=?",
                           (referrer_id, referred_id)).fetchone()
        if row:
            return False
        conn.execute("INSERT INTO referrals(referrer_id, referred_id, created_at) VALUES(?,?,?)",
                     (referrer_id, referred_id, _utcnow()))
        conn.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id=?", (referrer_id,))
        conn.commit()
        return True


def add_freepik_task(task_id: str, user_id: int, chat_id: int, kind: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO freepik_tasks(task_id, user_id, chat_id, kind, created_at)
            VALUES(?,?,?,?,?)
        """, (task_id, user_id, chat_id, kind, _utcnow()))
        conn.commit()


def get_freepik_task(task_id: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM freepik_tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None
