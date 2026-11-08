
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "assistant.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialise_valuation_profiles():

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS valuation_profiles (
                ticker TEXT PRIMARY KEY,
                growth_rate REAL NOT NULL,
                discount_rate REAL NOT NULL,
                terminal_growth_rate REAL NOT NULL,
                margin_of_safety REAL NOT NULL,
                forecast_years INTEGER NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def save_valuation_profile(
    ticker,
    growth_rate,
    discount_rate,
    terminal_growth_rate,
    margin_of_safety=0.25,
    forecast_years=5
):

    ticker = ticker.upper().strip()

    initialise_valuation_profiles()

    # Validate assumptions
    if not ticker:
        return "Error: Ticker cannot be empty."

    if growth_rate <= -1:
        return "Error: Growth rate must be greater than -100%."

    if discount_rate <= terminal_growth_rate:
        return "Error: Discount rate must exceed terminal growth rate."

    if discount_rate <= 0:
        return "Error: Discount rate must be positive."

    if not 0 <= margin_of_safety < 1:
        return "Error: Margin of safety must be between 0 and 1."

    if forecast_years < 1 or forecast_years > 20:
        return "Error: Forecast years must be between 1 and 20."

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO valuation_profiles (
                ticker,
                growth_rate,
                discount_rate,
                terminal_growth_rate,
                margin_of_safety,
                forecast_years
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(ticker)
            DO UPDATE SET
                growth_rate = excluded.growth_rate,
                discount_rate = excluded.discount_rate,
                terminal_growth_rate = excluded.terminal_growth_rate,
                margin_of_safety = excluded.margin_of_safety,
                forecast_years = excluded.forecast_years,
                updated_at = CURRENT_TIMESTAMP
        """, (
            ticker,
            growth_rate,
            discount_rate,
            terminal_growth_rate,
            margin_of_safety,
            forecast_years
        ))

    return f"Valuation profile saved for {ticker}."


def get_valuation_profile(ticker):

    ticker = ticker.upper().strip()

    initialise_valuation_profiles()

    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                ticker,
                growth_rate,
                discount_rate,
                terminal_growth_rate,
                margin_of_safety,
                forecast_years,
                updated_at
            FROM valuation_profiles
            WHERE ticker = ?
        """, (ticker,)).fetchone()

    if row is None:
        return None

    return {
        "ticker": row[0],
        "growth_rate": row[1],
        "discount_rate": row[2],
        "terminal_growth_rate": row[3],
        "margin_of_safety": row[4],
        "forecast_years": row[5],
        "updated_at": row[6]
    }


def get_all_valuation_profiles():

    initialise_valuation_profiles()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                ticker,
                growth_rate,
                discount_rate,
                terminal_growth_rate,
                margin_of_safety,
                forecast_years,
                updated_at
            FROM valuation_profiles
            ORDER BY ticker
        """).fetchall()

    return [
        {
            "ticker": row[0],
            "growth_rate": row[1],
            "discount_rate": row[2],
            "terminal_growth_rate": row[3],
            "margin_of_safety": row[4],
            "forecast_years": row[5],
            "updated_at": row[6]
        }
        for row in rows
    ]
