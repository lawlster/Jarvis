
import sqlite3
from pathlib import Path

# Database location
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_memory():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def save_message(role, content):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content)
        )


def load_recent_messages(limit=20):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT role, content
            FROM conversations
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    # Reverse to restore chronological order
    return [
        {"role": role, "content": content}
        for role, content in reversed(rows)
    ]


def clear_memory():
    with get_connection() as conn:
        conn.execute("DELETE FROM conversations")
