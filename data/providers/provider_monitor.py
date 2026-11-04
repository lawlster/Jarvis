
import sqlite3

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    return sqlite3.connect(DB_PATH)


def get_provider_performance():

    """
    Calculate performance statistics for each
    registered financial data provider.
    """

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT

                provider,

                COUNT(*) AS total_requests,

                SUM(
                    CASE
                        WHEN status = 'success'
                        THEN 1 ELSE 0
                    END
                ) AS successful_requests,

                SUM(
                    CASE
                        WHEN status = 'failed'
                        THEN 1 ELSE 0
                    END
                ) AS failed_requests,

                SUM(fallback_used) AS fallback_requests,

                AVG(quality_score) AS average_quality_score

            FROM provider_logs

            GROUP BY provider

            ORDER BY total_requests DESC

        """).fetchall()

    results = []

    for row in rows:

        provider = row[0]

        total = row[1] or 0
        successful = row[2] or 0
        failed = row[3] or 0
        fallback = row[4] or 0
        average_quality = row[5]

        success_rate = (
            successful / total * 100
            if total > 0
            else 0
        )

        results.append({

            "provider": provider,

            "total_requests": total,

            "successful_requests": successful,

            "failed_requests": failed,

            "fallback_requests": fallback,

            "success_rate": round(
                success_rate,
                2
            ),

            "average_quality_score": (
                round(average_quality, 2)
                if average_quality is not None
                else None
            )

        })

    return results


def get_provider_reliability_report():

    performance = get_provider_performance()

    if not performance:

        return {
            "message": "No provider activity has been recorded yet.",
            "providers": []
        }

    reports = []

    for provider in performance:

        success_rate = provider["success_rate"]

        quality_score = provider["average_quality_score"]

        if success_rate >= 95:
            reliability = "high"

        elif success_rate >= 80:
            reliability = "moderate"

        else:
            reliability = "low"

        if quality_score is None:
            quality_status = "not assessed"

        elif quality_score >= 85:
            quality_status = "good"

        elif quality_score >= 60:
            quality_status = "warning"

        else:
            quality_status = "poor"

        reports.append({

            **provider,

            "reliability": reliability,

            "quality_status": quality_status

        })

    return {
        "providers": reports,
        "provider_count": len(reports)
    }


def get_provider_failures(limit=20):

    limit = max(
        1,
        min(int(limit), 500)
    )

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT
                ticker,
                provider,
                error_message,
                retrieved_at

            FROM provider_logs

            WHERE status = 'failed'

            ORDER BY id DESC

            LIMIT ?

        """, (limit,)).fetchall()

    return [

        {
            "ticker": row[0],
            "provider": row[1],
            "error_message": row[2],
            "retrieved_at": row[3]
        }

        for row in rows

    ]


def get_provider_quality_issues(limit=20):

    limit = max(
        1,
        min(int(limit), 500)
    )

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT
                ticker,
                provider,
                quality_status,
                quality_score,
                retrieved_at

            FROM provider_logs

            WHERE status = 'success'
            AND quality_status IN ('warning', 'poor')

            ORDER BY id DESC

            LIMIT ?

        """, (limit,)).fetchall()

    return [

        {
            "ticker": row[0],
            "provider": row[1],
            "quality_status": row[2],
            "quality_score": row[3],
            "retrieved_at": row[4]
        }

        for row in rows

    ]
