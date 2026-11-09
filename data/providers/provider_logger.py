
import os
import sqlite3

from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parents[2]
# Step 52: the path honours JARVIS_DB_PATH, like agent/memory.py,
# so a session pointed at another database (``--db-path`` or a
# test copy) logs provider requests there instead of into the
# production database.  The default is unchanged.
DB_PATH = Path(os.environ.get("JARVIS_DB_PATH") or
               (BASE_DIR / "database" / "assistant.db"))


def _resolve_db_path(db_path=None):
    return Path(db_path) if db_path is not None else DB_PATH


def get_connection(db_path=None):

    path = _resolve_db_path(db_path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    return sqlite3.connect(path)


def initialise_provider_logs(db_path=None):

    with get_connection(db_path) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_logs (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ticker TEXT NOT NULL,

                provider TEXT NOT NULL,

                status TEXT NOT NULL,

                fallback_used INTEGER DEFAULT 0,

                quality_status TEXT,

                quality_score INTEGER,

                error_message TEXT,

                retrieved_at TEXT NOT NULL

            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_provider_logs_ticker
            ON provider_logs(ticker)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_provider_logs_provider
            ON provider_logs(provider)
        """)


def log_provider_request(
    ticker,
    provider,
    status,
    fallback_used=False,
    quality_status=None,
    quality_score=None,
    error_message=None,
    db_path=None
):

    initialise_provider_logs(db_path)

    retrieved_at = datetime.now(
        timezone.utc
    ).isoformat()

    with get_connection(db_path) as conn:

        cursor = conn.execute("""
            INSERT INTO provider_logs (

                ticker,
                provider,
                status,
                fallback_used,
                quality_status,
                quality_score,
                error_message,
                retrieved_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

        """, (
            ticker.upper().strip(),
            provider,
            status,
            int(fallback_used),
            quality_status,
            quality_score,
            error_message,
            retrieved_at
        ))

        return cursor.lastrowid


def get_recent_provider_logs(limit=50, db_path=None):

    initialise_provider_logs(db_path)

    limit = max(1, min(int(limit), 500))

    with get_connection(db_path) as conn:

        rows = conn.execute("""
            SELECT *
            FROM provider_logs
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    columns = [
        "id",
        "ticker",
        "provider",
        "status",
        "fallback_used",
        "quality_status",
        "quality_score",
        "error_message",
        "retrieved_at"
    ]

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def get_provider_log_summary(db_path=None):

    initialise_provider_logs(db_path)

    with get_connection(db_path) as conn:

        rows = conn.execute("""
            SELECT
                provider,
                COUNT(*) AS total_requests,

                SUM(
                    CASE WHEN status = 'success'
                    THEN 1 ELSE 0 END
                ) AS successful_requests,

                SUM(
                    CASE WHEN status = 'failed'
                    THEN 1 ELSE 0 END
                ) AS failed_requests,

                SUM(fallback_used) AS fallback_requests

            FROM provider_logs

            GROUP BY provider

            ORDER BY total_requests DESC

        """).fetchall()

    return [
        {
            "provider": row[0],
            "total_requests": row[1],
            "successful_requests": row[2],
            "failed_requests": row[3],
            "fallback_requests": row[4]
        }
        for row in rows
    ]
