
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_alerts():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS valuation_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_states (
                ticker TEXT PRIMARY KEY,
                last_status TEXT,
                updated_at TEXT NOT NULL
            )
        """)


def create_alert(ticker, alert_type, message):
    ticker = ticker.upper().strip()

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO valuation_alerts (
                ticker,
                alert_type,
                message,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            ticker,
            alert_type,
            message,
            datetime.now(timezone.utc).isoformat()
        ))


def get_alert_state(ticker):
    initialise_alerts()
    with get_connection() as conn:
        row = conn.execute("""
            SELECT last_status
            FROM alert_states
            WHERE ticker = ?
        """, (ticker.upper().strip(),)).fetchone()

    return row[0] if row else None


def update_alert_state(ticker, status):
    initialise_alerts()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO alert_states (
                ticker,
                last_status,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(ticker)
            DO UPDATE SET
                last_status = excluded.last_status,
                updated_at = excluded.updated_at
        """, (
            ticker.upper().strip(),
            status,
            datetime.now(timezone.utc).isoformat()
        ))


def process_valuation_alerts(classifications):
    initialise_alerts()

    if not isinstance(classifications, list):
        return {"error": "Invalid classification data."}

    alerts_created = []

    for item in classifications:

        ticker = item.get("ticker", "UNKNOWN").upper().strip()
        status = item.get("status", "insufficient_data")

        if status == "insufficient_data":
            continue

        previous_status = get_alert_state(ticker)

        if status == "below_threshold":

            if previous_status != "below_threshold":

                message = (
                    f"{ticker} has reached or fallen below "
                    "your predefined valuation threshold. "
                    "Review the underlying assumptions and data."
                )

                create_alert(
                    ticker,
                    "below_threshold",
                    message
                )

                alerts_created.append({
                    "ticker": ticker,
                    "alert_type": "below_threshold",
                    "message": message
                })

        elif (
            previous_status == "below_threshold"
            and status != "below_threshold"
        ):

            message = (
                f"{ticker} is no longer classified as "
                "below your predefined valuation threshold."
            )

            create_alert(
                ticker,
                "threshold_exit",
                message
            )

            alerts_created.append({
                "ticker": ticker,
                "alert_type": "threshold_exit",
                "message": message
            })

        update_alert_state(ticker, status)

    return {
        "alerts_created": len(alerts_created),
        "alerts": alerts_created
    }


def get_recent_alerts(limit=20):
    initialise_alerts()

    if not isinstance(limit, int) or limit < 1:
        return []

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                id,
                ticker,
                alert_type,
                message,
                created_at,
                acknowledged
            FROM valuation_alerts
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [
        {
            "id": row[0],
            "ticker": row[1],
            "alert_type": row[2],
            "message": row[3],
            "created_at": row[4],
            "acknowledged": bool(row[5])
        }
        for row in rows
    ]


def acknowledge_alert(alert_id):
    initialise_alerts()
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE valuation_alerts
            SET acknowledged = 1
            WHERE id = ?
        """, (alert_id,))

    return cursor.rowcount > 0
