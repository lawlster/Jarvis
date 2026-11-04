
import sqlite3

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]

DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    return conn


def initialise_statement_database():

    with get_connection() as conn:

        conn.executescript("""

            CREATE TABLE IF NOT EXISTS financial_statements (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ticker TEXT NOT NULL,

                company_name TEXT,

                exchange TEXT,

                country TEXT,

                statement_type TEXT NOT NULL,

                reporting_period TEXT NOT NULL,

                period_type TEXT NOT NULL,

                fiscal_year INTEGER,

                fiscal_quarter INTEGER,

                currency TEXT,

                source TEXT,

                source_document TEXT,

                data_json TEXT NOT NULL,

                collected_at TEXT NOT NULL,

                UNIQUE (
                    ticker,
                    exchange,
                    statement_type,
                    reporting_period,
                    period_type
                )

            );


            CREATE INDEX IF NOT EXISTS idx_statements_ticker

            ON financial_statements(ticker);


            CREATE INDEX IF NOT EXISTS idx_statements_period

            ON financial_statements(reporting_period);


            CREATE INDEX IF NOT EXISTS idx_statements_type

            ON financial_statements(statement_type);


            CREATE TABLE IF NOT EXISTS statement_collection_history (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                ticker TEXT NOT NULL,

                exchange TEXT,

                source TEXT,

                status TEXT NOT NULL,

                statements_collected INTEGER DEFAULT 0,

                error_message TEXT,

                started_at TEXT NOT NULL,

                completed_at TEXT

            );


            CREATE INDEX IF NOT EXISTS idx_collection_ticker

            ON statement_collection_history(ticker);


            CREATE INDEX IF NOT EXISTS idx_collection_status

            ON statement_collection_history(status);

        """)

    return True


def save_financial_statement(statement):

    required_fields = [
        "ticker",
        "statement_type",
        "reporting_period",
        "period_type",
        "data"
    ]

    for field in required_fields:

        if field not in statement or statement[field] is None:

            raise ValueError(
                f"Missing required field: {field}"
            )

    import json

    from datetime import datetime, timezone

    ticker = statement["ticker"].upper().strip()

    with get_connection() as conn:

        conn.execute("""

            INSERT INTO financial_statements (

                ticker,
                company_name,
                exchange,
                country,
                statement_type,
                reporting_period,
                period_type,
                fiscal_year,
                fiscal_quarter,
                currency,
                source,
                source_document,
                data_json,
                collected_at

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT (
                ticker,
                exchange,
                statement_type,
                reporting_period,
                period_type
            )

            DO UPDATE SET

                company_name = excluded.company_name,

                country = excluded.country,

                fiscal_year = excluded.fiscal_year,

                fiscal_quarter = excluded.fiscal_quarter,

                currency = excluded.currency,

                source = excluded.source,

                source_document = excluded.source_document,

                data_json = excluded.data_json,

                collected_at = excluded.collected_at

        """, (

            ticker,

            statement.get("company_name"),

            statement.get("exchange"),

            statement.get("country"),

            statement["statement_type"],

            statement["reporting_period"],

            statement["period_type"],

            statement.get("fiscal_year"),

            statement.get("fiscal_quarter"),

            statement.get("currency"),

            statement.get("source"),

            statement.get("source_document"),

            json.dumps(statement["data"]),

            datetime.now(timezone.utc).isoformat()

        ))

    return True


def get_company_statements(
    ticker,
    statement_type=None,
    period_type=None
):

    import json

    query = """

        SELECT *

        FROM financial_statements

        WHERE ticker = ?

    """

    parameters = [
        ticker.upper().strip()
    ]

    if statement_type:

        query += " AND statement_type = ?"

        parameters.append(statement_type)

    if period_type:

        query += " AND period_type = ?"

        parameters.append(period_type)

    query += """

        ORDER BY reporting_period DESC

    """

    with get_connection() as conn:

        rows = conn.execute(
            query,
            parameters
        ).fetchall()

    results = []

    for row in rows:

        item = dict(row)

        item["data"] = json.loads(
            item.pop("data_json")
        )

        results.append(item)

    return results


def get_statement_database_summary():

    with get_connection() as conn:

        total = conn.execute("""

            SELECT COUNT(*)

            FROM financial_statements

        """).fetchone()[0]

        companies = conn.execute("""

            SELECT COUNT(DISTINCT ticker)

            FROM financial_statements

        """).fetchone()[0]

        breakdown = conn.execute("""

            SELECT

                statement_type,

                period_type,

                COUNT(*) AS total

            FROM financial_statements

            GROUP BY statement_type, period_type

        """).fetchall()

    return {

        "total_statements": total,

        "companies_covered": companies,

        "breakdown": [

            {

                "statement_type": row[0],

                "period_type": row[1],

                "total": row[2]

            }

            for row in breakdown

        ]

    }
