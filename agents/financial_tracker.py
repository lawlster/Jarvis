
import sqlite3
import json
from pathlib import Path
from datetime import datetime

from tools.finance import get_company_financials


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def initialise_financial_tracker():

    with sqlite3.connect(DB_PATH) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS financial_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                snapshot_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                financial_data TEXT NOT NULL
            )
        """)


def save_financial_snapshot(ticker):

    ticker = ticker.upper().strip()

    initialise_financial_tracker()

    financials = get_company_financials(ticker)

    if "error" in financials:
        return financials

    with sqlite3.connect(DB_PATH) as conn:

        conn.execute("""
            INSERT INTO financial_snapshots
            (ticker, financial_data)
            VALUES (?, ?)
        """, (
            ticker,
            json.dumps(financials)
        ))

    return f"Financial snapshot saved for {ticker}."


def get_financial_history(ticker):

    ticker = ticker.upper().strip()

    initialise_financial_tracker()

    with sqlite3.connect(DB_PATH) as conn:

        rows = conn.execute("""
            SELECT snapshot_date, financial_data
            FROM financial_snapshots
            WHERE ticker = ?
            ORDER BY snapshot_date ASC
        """, (ticker,)).fetchall()

    return [
        {
            "snapshot_date": row[0],
            "financial_data": json.loads(row[1])
        }
        for row in rows
    ]
