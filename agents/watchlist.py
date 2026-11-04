
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_watchlist():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                company_name TEXT,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def add_company(ticker, company_name=None):
    ticker = ticker.upper().strip()

    if not ticker:
        return "Error: Ticker cannot be empty."

    with get_connection() as conn:
        try:
            conn.execute(
                """
                INSERT INTO watchlist (ticker, company_name)
                VALUES (?, ?)
                """,
                (ticker, company_name)
            )
            return f"{ticker} added to your watchlist."

        except sqlite3.IntegrityError:
            return f"{ticker} is already in your watchlist."


def remove_company(ticker):
    ticker = ticker.upper().strip()

    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM watchlist WHERE ticker = ?",
            (ticker,)
        )

        if cursor.rowcount == 0:
            return f"{ticker} was not found in your watchlist."

    return f"{ticker} removed from your watchlist."


def get_watchlist():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, company_name, added_at
            FROM watchlist
            ORDER BY ticker
        """).fetchall()

    return [
        {
            "ticker": row[0],
            "company_name": row[1],
            "added_at": row[2]
        }
        for row in rows
    ]
