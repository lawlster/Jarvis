
import sqlite3

from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    return sqlite3.connect(DB_PATH)


def initialise_provider_logs():

    with get_connection() as conn:

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
    error_message=None
):

    initialise_provider_logs()

    retrieved_at = datetime.now(
        timezone.utc
    ).isoformat()

    with get_connection() as conn:

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


def get_recent_provider_logs(limit=50):

    initialise_provider_logs()

    limit = max(1, min(int(limit), 500))

    with get_connection() as conn:

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


def get_provider_log_summary():

    initialise_provider_logs()

    with get_connection() as conn:

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
