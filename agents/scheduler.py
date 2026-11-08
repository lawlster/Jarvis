
import sqlite3

from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from agents.watchlist_valuation import evaluate_watchlist
from agents.alert_manager import process_valuation_alerts


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_scheduler_history():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                total_companies INTEGER DEFAULT 0,
                successful_valuations INTEGER DEFAULT 0,
                failed_valuations INTEGER DEFAULT 0,
                error_message TEXT
            )
        """)


def record_monitoring_run(
    started_at,
    completed_at,
    status,
    total_companies=0,
    successful_valuations=0,
    failed_valuations=0,
    error_message=None
):
    initialise_scheduler_history()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO monitoring_runs (
                started_at,
                completed_at,
                status,
                total_companies,
                successful_valuations,
                failed_valuations,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            started_at,
            completed_at,
            status,
            total_companies,
            successful_valuations,
            failed_valuations,
            error_message
        ))


def run_monitoring_cycle():
    started_at = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 65)
    print("AiAssistant - AUTOMATED MONITORING")
    print("=" * 65)
    print(f"Started: {started_at}")

    try:
        report = evaluate_watchlist()

        if not isinstance(report, dict):
            raise ValueError("Invalid monitoring report returned.")

        if "error" in report:
            raise ValueError(report["error"])

        classifications = report.get("classifications", [])

        alert_report = process_valuation_alerts(classifications)

        if "error" in alert_report:
            print("Alert processing warning:", alert_report["error"])

        completed_at = datetime.now(timezone.utc).isoformat()

        successful = report.get("successful_valuations", 0)
        failed = report.get("failed_valuations", 0)

        record_monitoring_run(
            started_at=started_at,
            completed_at=completed_at,
            status="completed",
            total_companies=report.get("total_companies", 0),
            successful_valuations=successful,
            failed_valuations=failed
        )

        print("\nMonitoring cycle completed.")
        print(f"Successful valuations: {successful}")
        print(f"Failed valuations: {failed}")

        if isinstance(alert_report, dict):
            print(
                "New alerts created:",
                alert_report.get("alerts_created", 0)
            )

            for alert in alert_report.get("alerts", []):
                print(f"ALERT: {alert['message']}")

        return {
            "monitoring_report": report,
            "alert_report": alert_report
        }

    except Exception as error:

        completed_at = datetime.now(timezone.utc).isoformat()

        record_monitoring_run(
            started_at=started_at,
            completed_at=completed_at,
            status="failed",
            error_message=str(error)
        )

        print(f"\nMonitoring cycle failed: {error}")

        return {"error": str(error)}


def start_monitoring(interval_hours=24):
    if not isinstance(interval_hours, (int, float)):
        raise ValueError("Interval must be a number.")

    if interval_hours <= 0:
        raise ValueError("Interval must be greater than zero.")

    initialise_scheduler_history()

    scheduler = BackgroundScheduler(timezone="Asia/Singapore")

    scheduler.add_job(
        run_monitoring_cycle,
        trigger="interval",
        hours=interval_hours,
        id="watchlist_monitoring",
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )

    scheduler.start()

    print("\nAiAssistant background monitoring started.")
    print(f"Monitoring interval: Every {interval_hours} hours.")
    print("The first automatic run will happen after the interval.")
    print("Keep AiAssistant running for scheduled checks to execute.")

    return scheduler


def get_monitoring_history(limit=20):
    if not isinstance(limit, int) or limit < 1:
        return []

    initialise_scheduler_history()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                started_at,
                completed_at,
                status,
                total_companies,
                successful_valuations,
                failed_valuations,
                error_message
            FROM monitoring_runs
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [
        {
            "started_at": row[0],
            "completed_at": row[1],
            "status": row[2],
            "total_companies": row[3],
            "successful_valuations": row[4],
            "failed_valuations": row[5],
            "error_message": row[6]
        }
        for row in rows
    ]
