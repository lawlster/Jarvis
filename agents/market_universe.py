
import sqlite3

from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


SUPPORTED_EXCHANGES = [
    "NYSE",
    "NASDAQ",
    "NYSE_AMERICAN",
    "SGX",
    "HKEX"
]

SUPPORTED_COUNTRIES = [
    "USA",
    "Singapore",
    "Hong Kong"
]

SUPPORTED_CURRENCIES = [
    "USD",
    "SGD",
    "HKD"
]

SUPPORTED_SECURITY_TYPES = [
    "equity",
    "reit",
    "financial"
]


def get_connection():

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    return sqlite3.connect(DB_PATH)


def initialise_market_universe():

    with get_connection() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_universe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                company_name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                country TEXT NOT NULL,
                currency TEXT NOT NULL,
                sector TEXT,
                industry TEXT,
                security_type TEXT DEFAULT 'equity',
                market_cap REAL,
                is_active INTEGER DEFAULT 1,
                data_source TEXT,
                last_updated TEXT NOT NULL,
                sec_cik TEXT,
                UNIQUE(ticker, exchange)
            )
        """)

        # Migrate databases created before the SEC CIK field existed.
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(market_universe)"
            ).fetchall()
        }

        if "sec_cik" not in columns:

            conn.execute("""
                ALTER TABLE market_universe
                ADD COLUMN sec_cik TEXT
            """)


def add_company(
    ticker,
    company_name,
    exchange,
    country,
    currency,
    sector=None,
    industry=None,
    security_type="equity",
    market_cap=None,
    data_source=None,
    sec_cik=None
):

    ticker = str(ticker).upper().strip()
    company_name = str(company_name).strip()

    if not ticker or not company_name:

        return {
            "error": "Ticker and company name are required."
        }

    if exchange not in SUPPORTED_EXCHANGES:

        return {
            "error": "Unsupported exchange."
        }

    if country not in SUPPORTED_COUNTRIES:

        return {
            "error": "Unsupported country."
        }

    if currency not in SUPPORTED_CURRENCIES:

        return {
            "error": "Unsupported currency."
        }

    if security_type not in SUPPORTED_SECURITY_TYPES:

        return {
            "error": "Unsupported security type."
        }

    if market_cap is not None and market_cap < 0:

        return {
            "error": "Market capitalisation cannot be negative."
        }

    if sec_cik is not None:

        sec_cik = str(sec_cik).zfill(10)

    updated_at = datetime.now(timezone.utc).isoformat()

    initialise_market_universe()

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO market_universe (
                ticker,
                company_name,
                exchange,
                country,
                currency,
                sector,
                industry,
                security_type,
                market_cap,
                is_active,
                data_source,
                last_updated,
                sec_cik
            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)

            ON CONFLICT(ticker, exchange)

            DO UPDATE SET
                company_name = excluded.company_name,
                country = excluded.country,
                currency = excluded.currency,
                sector = COALESCE(
                    excluded.sector,
                    market_universe.sector
                ),
                industry = COALESCE(
                    excluded.industry,
                    market_universe.industry
                ),
                -- Generic rediscovery defaults to equity; retain a reviewed
                -- non-equity classification on an existing listing.
                security_type = CASE
                    WHEN market_universe.security_type IS NOT NULL
                     AND market_universe.security_type != 'equity'
                    THEN market_universe.security_type
                    ELSE excluded.security_type
                END,
                market_cap = COALESCE(
                    excluded.market_cap,
                    market_universe.market_cap
                ),
                is_active = 1,
                data_source = excluded.data_source,
                last_updated = excluded.last_updated,
                sec_cik = COALESCE(
                    excluded.sec_cik,
                    market_universe.sec_cik
                )

        """, (
            ticker,
            company_name,
            exchange,
            country,
            currency,
            sector,
            industry,
            security_type,
            market_cap,
            data_source,
            updated_at,
            sec_cik
        ))

    return {
        "success": True,
        "ticker": ticker,
        "exchange": exchange,
        "sec_cik": sec_cik,
        "message": f"{ticker} saved to market universe."
    }


def get_market_universe(
    country=None,
    exchange=None,
    active_only=True
):

    query = """
        SELECT
            id,
            ticker,
            company_name,
            exchange,
            country,
            currency,
            sector,
            industry,
            security_type,
            market_cap,
            is_active,
            data_source,
            last_updated,
            sec_cik

        FROM market_universe

        WHERE 1=1
    """

    parameters = []

    if country:

        query += " AND country = ?"
        parameters.append(country)

    if exchange:

        query += " AND exchange = ?"
        parameters.append(exchange)

    if active_only:

        query += " AND is_active = 1"

    query += " ORDER BY country, exchange, ticker"

    with get_connection() as conn:

        rows = conn.execute(
            query,
            parameters
        ).fetchall()

    columns = [
        "id",
        "ticker",
        "company_name",
        "exchange",
        "country",
        "currency",
        "sector",
        "industry",
        "security_type",
        "market_cap",
        "is_active",
        "data_source",
        "last_updated",
        "sec_cik"
    ]

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def get_market_universe_summary():

    initialise_market_universe()

    with get_connection() as conn:

        rows = conn.execute("""
            SELECT
                country,
                exchange,
                COUNT(*)

            FROM market_universe

            WHERE is_active = 1

            GROUP BY country, exchange

            ORDER BY country, exchange

        """).fetchall()

    return [
        {
            "country": row[0],
            "exchange": row[1],
            "company_count": row[2]
        }
        for row in rows
    ]


def deactivate_company(ticker, exchange):

    with get_connection() as conn:

        cursor = conn.execute("""
            UPDATE market_universe

            SET is_active = 0

            WHERE ticker = ?
            AND exchange = ?

        """, (
            ticker.upper().strip(),
            exchange
        ))

    return cursor.rowcount > 0
