import os
import sqlite3
from pathlib import Path

# Database location.  Step 52: the path can be overridden with
# the JARVIS_DB_PATH environment variable (used by subprocess
# end-to-end runs and by the owner to point the assistant at a
# different database).  The default is unchanged — the project's
# production database — and no other behaviour changes.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("JARVIS_DB_PATH") or
               (BASE_DIR / "database" / "assistant.db"))


def _resolve_db_path(db_path=None):
    """Database path for this call: explicit argument wins, then
    the module default (which itself honours JARVIS_DB_PATH)."""
    return Path(db_path) if db_path is not None else DB_PATH


class _ClosingConnection(sqlite3.Connection):
    """A connection that closes when its ``with`` block ends.

    Step 61: a plain ``sqlite3.Connection`` only commits or rolls
    back on ``__exit__`` — it does NOT close.  Every
    ``with get_connection() as conn:`` site therefore leaked one
    open connection (one open file handle) for the life of the
    process.  Linux tolerates that; Windows does not — the open
    handles block deleting or replacing the database file, and a
    test run ends in a wall of WinError 32 PermissionErrors.
    This subclass commits/rolls back exactly like the base class,
    then closes, so all existing ``with`` sites now close their
    connection with no call-site change.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
        return False


def get_connection(db_path=None):
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, factory=_ClosingConnection)


def initialise_memory(db_path=None):
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def save_message(role, content, db_path=None):
    initialise_memory(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content)
        )


def load_recent_messages(limit=20, db_path=None):
    initialise_memory(db_path)
    with get_connection(db_path) as conn:
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


def clear_memory(db_path=None):
    initialise_memory(db_path)
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM conversations")
