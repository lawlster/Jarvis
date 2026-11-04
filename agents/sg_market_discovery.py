
import requests
import logging

from datetime import datetime, timezone

from agents.market_universe import (
    initialise_market_universe,
    add_company,
    get_market_universe_summary
)


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

COMPANIES_URL = (
    "https://stocks.com.sg/api/v1/companies"
)

SCREENER_URL = (
    "https://stocks.com.sg/api/v1/screener"
)

DATA_SOURCE = (
    "stocks.com.sg API (third-party discovery feed)"
)

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "AiAssistant/2.0"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# API HELPERS
# --------------------------------------------------

def fetch_endpoint(url, params=None):

    try:

        response = SESSION.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):

            return {
                "error": "Unexpected API response format."
            }

        if payload.get("error"):

            return {
                "error": payload["error"]
            }

        records = payload.get("data")

        if not isinstance(records, list):

            return {
                "error": "API response does not contain a data list."
            }

        return records

    except requests.RequestException as error:

        return {
            "error": str(error)
        }

    except ValueError as error:

        return {
            "error": f"Invalid JSON response: {error}"
        }


# --------------------------------------------------
# COMPANY DIRECTORY
# --------------------------------------------------

def fetch_sg_company_listings():

    """
    Retrieve the provider's company directory.

    This endpoint currently returns a limited directory.
    It is used as a supplementary source.
    """

    return fetch_endpoint(
        COMPANIES_URL
    )


# --------------------------------------------------
# SCREENER DIRECTORY
# --------------------------------------------------

def fetch_sg_screener_listings():

    """
    Retrieve the broader SGX screener directory.

    The screener currently returns approximately
    202 unique records based on our testing.
    """

    return fetch_endpoint(
        SCREENER_URL
    )


# --------------------------------------------------
# NAME AND TICKER NORMALISATION
# --------------------------------------------------

def normalise_ticker(value):

    if value is None:
        return ""

    return str(value).strip().upper()


def normalise_name(value):

    if not value:
        return ""

    return " ".join(
        str(value).upper().split()
    )


# --------------------------------------------------
# SECURITY CLASSIFICATION
# --------------------------------------------------

def classify_security(company):

    """
    Classify supported SGX security types.

    Supported database types:
        equity
        reit
        financial
    """

    name = normalise_name(
        company.get("company_name")
        or company.get("name")
    )

    sector = normalise_name(
        company.get("gics_sector")
        or company.get("sector")
    )

    industry = normalise_name(
        company.get("gics_industry")
        or company.get("industry")
    )

    security_type = normalise_name(
        company.get("security_type")
        or company.get("type")
    )

    combined = " ".join([
        name,
        sector,
        industry,
        security_type
    ])

    # REITs
    if (
        "REIT" in combined
        or "REAL ESTATE INVESTMENT TRUST" in combined
    ):

        return "reit"

    # Banks, insurers and other financial institutions
    if (
        sector == "FINANCIALS"
        or any(
            keyword in combined
            for keyword in [
                "BANKING",
                "INSURANCE",
                "FINANCIAL SERVICES"
            ]
        )
    ):

        return "financial"

    return "equity"


# --------------------------------------------------
# NON-COMPANY FILTERING
# --------------------------------------------------

EXCLUDED_SECURITY_KEYWORDS = [

    "ETF",

    "EXCHANGE TRADED FUND",

    "WARRANT",

    "DEBENTURE",

    "BOND",

    "INDEX",

    "STRUCTURED WARRANT",

    "DEPOSITORY RECEIPT"

]


def is_eligible_company(company):

    ticker = normalise_ticker(
        company.get("ticker")
        or company.get("symbol")
    )

    name = normalise_name(
        company.get("company_name")
        or company.get("name")
    )

    sector = normalise_name(
        company.get("gics_sector")
        or company.get("sector")
    )

    board = normalise_name(
        company.get("board")
    )

    if not ticker or not name:

        return False, "Missing ticker or company name"

    if ticker.startswith("^"):

        return False, "Index ticker"

    if sector == "INDEX":

        return False, "Index classification"

    if board == "BENCHMARK":

        return False, "Benchmark listing"

    security_description = " ".join([
        name,
        sector,
        normalise_name(
            company.get("gics_industry")
        ),
        normalise_name(
            company.get("security_type")
        ),
        normalise_name(
            company.get("type")
        )
    ])

    for keyword in EXCLUDED_SECURITY_KEYWORDS:

        if keyword in security_description:

            return False, (
                f"Unsupported instrument: {keyword}"
            )

    return True, None


# --------------------------------------------------
# CURRENCY
# --------------------------------------------------

def get_currency(company, directory_record=None):

    """
    Prefer explicitly supplied reporting currency.

    If unavailable, use SGD provisionally and flag
    the assumption in the collection result.
    """

    candidates = [

        company.get("reporting_currency"),

        company.get("currency"),

        company.get("trading_currency")

    ]

    if directory_record:

        candidates.extend([

            directory_record.get("reporting_currency"),

            directory_record.get("currency"),

            directory_record.get("trading_currency")

        ])

    for value in candidates:

        if value:

            currency = str(value).upper().strip()

            if currency in ["SGD", "USD"]:

                return currency, False

    return "SGD", True


# --------------------------------------------------
# MARKET CAPITALISATION
# --------------------------------------------------

def fetch_market_cap(company):

    value = (

        company.get("market_cap_sgd")

        or company.get("market_cap")

    )

    if value is None:
        return None

    try:

        result = float(value)

        if result >= 0:
            return result

    except (ValueError, TypeError):

        pass

    return None


# --------------------------------------------------
# RECORD MERGING
# --------------------------------------------------

def merge_company_sources(
    screener_records,
    directory_records
):

    """
    Merge both provider sources using ticker as
    the unique identifier.

    Screener data takes priority because it contains
    the broader set of financial and company fields.
    """

    merged = {}

    directory_by_ticker = {}

    for company in directory_records:

        if not isinstance(company, dict):
            continue

        ticker = normalise_ticker(
            company.get("ticker")
            or company.get("symbol")
        )

        if ticker:

            directory_by_ticker[ticker] = company

    for company in screener_records:

        if not isinstance(company, dict):
            continue

        ticker = normalise_ticker(
            company.get("ticker")
            or company.get("symbol")
        )

        if not ticker:
            continue

        merged[ticker] = dict(company)

    for ticker, company in directory_by_ticker.items():

        if ticker not in merged:

            merged[ticker] = dict(company)

        else:

            # Preserve directory currency information
            # without overwriting screener company identity.

            merged[ticker]["_directory_record"] = company

    return list(merged.values())


# --------------------------------------------------
# DATABASE IMPORT
# --------------------------------------------------

def import_sg_company_listings():

    """
    Import SGX companies into market_universe.

    Existing US records are not modified.
    Existing SGX records are updated by ticker.
    """

    initialise_market_universe()

    logger.info(
        "Starting Singapore market discovery."
    )

    # Fetch screener
    screener_records = fetch_sg_screener_listings()

    if isinstance(screener_records, dict):

        return {
            "status": "failed",
            "error": (
                "Screener retrieval failed: "
                + str(screener_records["error"])
            )
        }

    # Fetch supplementary directory
    directory_records = fetch_sg_company_listings()

    if isinstance(directory_records, dict):

        logger.warning(
            "Company directory unavailable: %s",
            directory_records["error"]
        )

        directory_records = []

    # Merge both sources
    companies = merge_company_sources(
        screener_records,
        directory_records
    )

    imported = 0
    failed = 0
    excluded = 0
    missing_ticker_or_name = 0
    currency_assumed = 0

    excluded_reasons = {}
    errors = []
    currency_assumptions = []

    for company in companies:

        ticker = normalise_ticker(
            company.get("ticker")
            or company.get("symbol")
        )

        company_name = str(
            company.get("company_name")
            or company.get("name")
            or ""
        ).strip()

        if not ticker or not company_name:

            missing_ticker_or_name += 1
            continue

        eligible, reason = is_eligible_company(
            company
        )

        if not eligible:

            excluded += 1

            excluded_reasons[reason] = (
                excluded_reasons.get(reason, 0) + 1
            )

            continue

        directory_record = company.get(
            "_directory_record"
        )

        currency, assumed = get_currency(
            company,
            directory_record
        )

        if assumed:

            currency_assumed += 1

            currency_assumptions.append(
                ticker
            )

        sector = (
            company.get("gics_sector")
            or company.get("sector")
        )

        industry = (
            company.get("gics_industry")
            or company.get("industry")
        )

        security_type = classify_security(
            company
        )

        market_cap = fetch_market_cap(
            company
        )

        try:

            result = add_company(

                ticker=ticker,

                company_name=company_name,

                exchange="SGX",

                country="Singapore",

                currency=currency,

                sector=sector,

                industry=industry,

                security_type=security_type,

                market_cap=market_cap,

                data_source=DATA_SOURCE

            )

            if result.get("success"):

                imported += 1

            else:

                failed += 1

                errors.append({

                    "ticker": ticker,

                    "error": result.get(
                        "error",
                        "Unknown database error"
                    )

                })

        except Exception as error:

            failed += 1

            errors.append({

                "ticker": ticker,

                "error": str(error)

            })

            logger.exception(
                "Failed importing %s",
                ticker
            )

    # Verify DBS explicitly
    dbs_present = False

    from agents.market_universe import get_market_universe

    current_universe = get_market_universe(
        country="Singapore",
        exchange="SGX",
        active_only=True
    )

    dbs_present = any(

        item["ticker"] == "D05"

        for item in current_universe

    )

    result = {

        "status": (
            "success"
            if imported > 0
            else "no_records_imported"
        ),

        "source": DATA_SOURCE,

        "screener_records_received": len(
            screener_records
        ),

        "company_directory_records_received": len(
            directory_records
        ),

        "unique_records_after_merging": len(
            companies
        ),

        "companies_imported_or_updated": imported,

        "excluded": excluded,

        "excluded_reasons": excluded_reasons,

        "failed": failed,

        "errors": errors[:20],

        "records_missing_ticker_or_name": (
            missing_ticker_or_name
        ),

        "records_with_assumed_sgd_currency": (
            currency_assumed
        ),

        "tickers_with_assumed_currency": (
            currency_assumptions[:30]
        ),

        "dbs_d05_present": dbs_present,

        "database_summary": (
            get_market_universe_summary()
        ),

        "coverage_warning": (

            "Third-party discovery data is not an "
            "official or complete SGX listing register. "

            "The screener returned 202 records in the "
            "latest diagnostic. "

            "Listing completeness, issuer identities, "
            "security classifications and currencies "
            "still require independent verification."

        ),

        "completed_at": datetime.now(
            timezone.utc
        ).isoformat()

    }

    logger.info(
        "Singapore discovery completed: %s",
        result
    )

    return result


# --------------------------------------------------
# DIRECT EXECUTION
# --------------------------------------------------

if __name__ == "__main__":

    import json

    result = import_sg_company_listings()

    print(
        json.dumps(
            result,
            indent=4
        )
    )
