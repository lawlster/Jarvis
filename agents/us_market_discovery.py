
import os

import requests
from dotenv import load_dotenv

from pathlib import Path

from agents.market_universe import (
    initialise_market_universe,
    add_company,
    get_market_universe_summary
)


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SEC_TICKER_URL = (
    "https://www.sec.gov/files/company_tickers_exchange.json"
)

# Step 52: the SEC contact address used to live here as a
# hardcoded personal email in tracked code.  It now comes from
# SEC_USER_AGENT in .env (the same pattern as
# agents/us_issuer_enrichment.py and
# data/financial_statements/sec_collector.py) and is empty by
# default — fetch_us_company_listings() refuses to contact the
# SEC until it is configured, instead of sending a request
# with a placeholder or someone else's address.
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()

SUPPORTED_EXCHANGES = {
    "nasdaq": "NASDAQ",
    "nyse": "NYSE",
    "nyse american": "NYSE_AMERICAN"
}


def fetch_us_company_listings():
    """Download the SEC company ticker and exchange dataset."""

    if not SEC_USER_AGENT:
        return {
            "error": "SEC_USER_AGENT is not configured. "
                     "Configure SEC_USER_AGENT in .env before "
                     "contacting SEC EDGAR."
        }

    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json"
    }

    try:
        response = requests.get(
            SEC_TICKER_URL,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()

        dataset = response.json()

        fields = dataset.get("fields", [])
        rows = dataset.get("data", [])

        if not fields or not rows:
            return {
                "error": "SEC dataset returned an unexpected format."
            }

        companies = []

        for row in rows:

            if len(row) != len(fields):
                continue

            company = dict(zip(fields, row))

            exchange_name = str(
                company.get("exchange", "")
            ).strip().lower()

            exchange = SUPPORTED_EXCHANGES.get(exchange_name)

            if not exchange:
                continue

            ticker = str(
                company.get("ticker", "")
            ).strip().upper()

            company_name = str(
                company.get("name", "")
            ).strip()

            cik = company.get("cik")

            if not ticker or not company_name or not cik:
                continue

            try:
                cik = str(int(cik)).zfill(10)
            except (ValueError, TypeError):
                continue

            companies.append({
                "ticker": ticker,
                "company_name": company_name,
                "exchange": exchange,
                "country": "USA",
                "currency": "USD",
                "sec_cik": cik
            })

        return companies

    except requests.RequestException as error:

        return {
            "error": f"SEC download failed: {error}"
        }

    except (ValueError, TypeError, AttributeError) as error:

        return {
            "error": f"Unable to process SEC dataset: {error}"
        }


def import_us_company_listings():
    """Import supported US company listings into SQLite."""

    initialise_market_universe()

    companies = fetch_us_company_listings()

    if isinstance(companies, dict) and "error" in companies:
        return companies

    imported = 0
    failed = 0
    exchange_counts = {
        "NYSE": 0,
        "NASDAQ": 0,
        "NYSE_AMERICAN": 0
    }

    for company in companies:

        result = add_company(
            ticker=company["ticker"],
            company_name=company["company_name"],
            exchange=company["exchange"],
            country=company["country"],
            currency=company["currency"],
            # Default new SEC listings to equity; the universe upsert keeps
            # any existing reviewed non-equity classification.
            security_type=company.get("security_type", "equity"),
            data_source="SEC EDGAR company_tickers_exchange.json",
            sec_cik=company["sec_cik"]
        )

        if result.get("success"):
            imported += 1
            exchange_counts[company["exchange"]] += 1
        else:
            failed += 1

    return {
        "source": "SEC EDGAR",
        "companies_processed": len(companies),
        "companies_imported_or_updated": imported,
        "failed": failed,
        "exchange_counts": exchange_counts,
        "database_summary": get_market_universe_summary()
    }
