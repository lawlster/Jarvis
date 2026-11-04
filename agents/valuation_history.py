
import sqlite3
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_valuation_history():

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS valuation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                snapshot_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                current_price REAL,
                intrinsic_value REAL,
                threshold_price REAL,
                classification TEXT,
                valuation_data TEXT NOT NULL
            )
        """)


def save_valuation_snapshot(ticker, valuation, classification=None):

    ticker = ticker.upper().strip()

    if not isinstance(valuation, dict):
        return "Invalid valuation data."

    current_price = valuation.get("current_price")
    intrinsic_value = valuation.get("estimated_intrinsic_value")
    threshold_price = valuation.get("threshold_price")

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO valuation_snapshots (
                ticker,
                current_price,
                intrinsic_value,
                threshold_price,
                classification,
                valuation_data
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            ticker,
            current_price,
            intrinsic_value,
            threshold_price,
            classification,
            json.dumps(valuation, allow_nan=False)
        ))

    return f"Valuation snapshot saved for {ticker}."


def save_watchlist_valuation_history(report):

    if not isinstance(report, dict) or "results" not in report:
        return {"error": "Invalid watchlist valuation report."}

    classifications = {
        item["ticker"]: item.get("status")
        for item in report.get("classifications", [])
    }

    saved = 0
    failed = 0
    errors = []

    for result in report["results"]:

        ticker = result.get("ticker", "").upper().strip()
        valuation = result.get("valuation", {})

        if not ticker or not isinstance(valuation, dict):
            failed += 1
            errors.append(f"Invalid result for {ticker or 'unknown company'}.")
            continue

        try:
            save_valuation_snapshot(
                ticker=ticker,
                valuation=valuation,
                classification=classifications.get(ticker)
            )
            saved += 1

        except (ValueError, TypeError, sqlite3.Error) as error:
            failed += 1
            errors.append(f"{ticker}: {error}")

    return {
        "saved": saved,
        "failed": failed,
        "errors": errors
    }


def get_valuation_history(ticker, limit=50):

    ticker = ticker.upper().strip()

    if not isinstance(limit, int) or limit < 1:
        return []

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT
                snapshot_date,
                current_price,
                intrinsic_value,
                threshold_price,
                classification,
                valuation_data
            FROM valuation_snapshots
            WHERE ticker = ?
            ORDER BY id DESC
            LIMIT ?
        """, (ticker, limit)).fetchall()

    return [
        {
            "snapshot_date": row[0],
            "current_price": row[1],
            "intrinsic_value": row[2],
            "threshold_price": row[3],
            "classification": row[4],
            "valuation_data": json.loads(row[5])
        }
        for row in reversed(rows)
    ]


def get_all_valuation_history(limit=100):

    if not isinstance(limit, int) or limit < 1:
        return []

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT
                ticker,
                snapshot_date,
                current_price,
                intrinsic_value,
                threshold_price,
                classification
            FROM valuation_snapshots
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [
        {
            "ticker": row[0],
            "snapshot_date": row[1],
            "current_price": row[2],
            "intrinsic_value": row[3],
            "threshold_price": row[4],
            "classification": row[5]
        }
        for row in reversed(rows)
    ]
