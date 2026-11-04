
import os
import time
import requests

from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

from data.financial_statements.statement_database import (
    initialise_statement_database,
    save_financial_statement,
    get_connection
)


BASE_DIR = Path(__file__).resolve().parents[2]

load_dotenv(BASE_DIR / ".env")

SEC_COMPANY_FACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json"
)

REQUEST_DELAY_SECONDS = 0.15


# Standard SEC US-GAAP accounting concepts.
STATEMENT_FIELDS = {

    "income_statement": {

        "revenue": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax"
        ],

        "cost_of_revenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold"
        ],

        "gross_profit": [
            "GrossProfit"
        ],

        "research_and_development": [
            "ResearchAndDevelopmentExpense"
        ],

        "selling_general_administrative": [
            "SellingGeneralAndAdministrativeExpense"
        ],

        "operating_income": [
            "OperatingIncomeLoss"
        ],

        "income_before_tax": [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"
        ],

        "income_tax": [
            "IncomeTaxExpenseBenefit"
        ],

        "net_income": [
            "NetIncomeLoss",
            "ProfitLoss"
        ],

        "basic_eps": [
            "EarningsPerShareBasic"
        ],

        "diluted_eps": [
            "EarningsPerShareDiluted"
        ],

        "basic_shares": [
            "WeightedAverageNumberOfSharesOutstandingBasic"
        ],

        "diluted_shares": [
            "WeightedAverageNumberOfDilutedSharesOutstanding"
        ]

    },

    "balance_sheet": {

        "cash_and_equivalents": [
            "CashAndCashEquivalentsAtCarryingValue"
        ],

        "short_term_investments": [
            "ShortTermInvestments"
        ],

        "accounts_receivable": [
            "AccountsReceivableNetCurrent"
        ],

        "inventory": [
            "InventoryNet"
        ],

        "current_assets": [
            "AssetsCurrent"
        ],

        "total_assets": [
            "Assets"
        ],

        "accounts_payable": [
            "AccountsPayableCurrent"
        ],

        "current_liabilities": [
            "LiabilitiesCurrent"
        ],

        "short_term_debt": [
            "LongTermDebtCurrent",
            "ShortTermBorrowings"
        ],

        "long_term_debt": [
            "LongTermDebtNoncurrent",
            "LongTermDebt"
        ],

        "total_liabilities": [
            "Liabilities"
        ],

        "shareholders_equity": [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
        ]

    },

    "cash_flow_statement": {

        "operating_cash_flow": [
            "NetCashProvidedByUsedInOperatingActivities"
        ],

        "capital_expenditure": [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets"
        ],

        "investing_cash_flow": [
            "NetCashProvidedByUsedInInvestingActivities"
        ],

        "financing_cash_flow": [
            "NetCashProvidedByUsedInFinancingActivities"
        ],

        "dividends_paid": [
            "PaymentsOfDividends",
            "PaymentsOfDividendsCommonStock"
        ],

        "share_repurchases": [
            "PaymentsForRepurchaseOfCommonStock",
            "PaymentsForRepurchaseOfEquity"
        ],

        "share_based_compensation": [
            "ShareBasedCompensation"
        ]

    }

}


ANNUAL_FORMS = {"10-K", "20-F"}
QUARTERLY_FORMS = {"10-Q"}


def get_sec_headers():

    user_agent = os.getenv("SEC_USER_AGENT", "").strip()

    if not user_agent or "your-email@example.com" in user_agent:

        raise ValueError(
            "Please configure SEC_USER_AGENT in your .env file."
        )

    return {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate"
    }


def get_company_record(ticker):

    ticker = ticker.upper().strip()

    with get_connection() as conn:

        rows = conn.execute("""

            SELECT
                ticker,
                company_name,
                exchange,
                country,
                currency,
                sec_cik

            FROM market_universe

            WHERE UPPER(ticker) = ?
            AND UPPER(country) = 'USA'
            AND is_active = 1
            AND sec_cik IS NOT NULL

        """, (ticker,)).fetchall()

    if not rows:

        raise ValueError(
            f"{ticker} was not found in the US market universe "
            "with an SEC CIK."
        )

    if len(rows) > 1:

        exchanges = {
            row["exchange"] for row in rows
        }

        if len(exchanges) > 1:

            raise ValueError(
                f"Multiple exchange records found for {ticker}. "
                "An exchange-specific selection is required."
            )

    return dict(rows[0])


def fetch_company_facts(cik):

    cik = str(cik).strip().zfill(10)

    url = SEC_COMPANY_FACTS_URL.format(cik)

    response = requests.get(
        url,
        headers=get_sec_headers(),
        timeout=30
    )

    response.raise_for_status()

    time.sleep(REQUEST_DELAY_SECONDS)

    return response.json()


def get_fact_units(concept):

    units = concept.get("units", {})

    if not units:
        return []

    preferred_units = [
        "USD",
        "shares",
        "USD/shares",
        "USD / shares"
    ]

    results = []

    for unit in preferred_units:

        if unit in units:

            results.extend(
                (unit, fact)
                for fact in units[unit]
            )

    if results:
        return results

    for unit, records in units.items():

        results.extend(
            (unit, fact)
            for fact in records
        )

    return results


def get_period_type(form, start, end):

    if not start:
        return None

    start_date = datetime.strptime(
        start, "%Y-%m-%d"
    )

    end_date = datetime.strptime(
        end, "%Y-%m-%d"
    )

    days = (end_date - start_date).days

    if form in ANNUAL_FORMS:

        if 300 <= days <= 400:
            return "annual"

    elif form in QUARTERLY_FORMS:

        if 70 <= days <= 100:
            return "quarterly"

    return None


def get_fact_records(
    facts,
    tags,
    period_type,
    statement_type
):

    selected_records = {}

    for tag in tags:

        concept = facts.get(tag)

        if not concept:
            continue

        for unit, fact in get_fact_units(concept):

            form = fact.get("form")

            if statement_type == "balance_sheet":

                if form in ANNUAL_FORMS:
                    fact_period_type = "annual"

                elif form in QUARTERLY_FORMS:
                    fact_period_type = "quarterly"

                else:
                    continue

                if fact_period_type != period_type:
                    continue

                end = fact.get("end")

                if not end:
                    continue

            else:

                if form not in (
                    ANNUAL_FORMS | QUARTERLY_FORMS
                ):
                    continue

                start = fact.get("start")
                end = fact.get("end")

                if not start or not end:
                    continue

                fact_period_type = get_period_type(
                    form,
                    start,
                    end
                )

                if fact_period_type != period_type:
                    continue

            record = {

                "value": fact.get("val"),

                "unit": unit,

                "tag": tag,

                "start": fact.get("start"),

                "end": end,

                "filed": fact.get("filed"),

                "form": form,

                "accession_number": fact.get("accn"),

                "fiscal_year": fact.get("fy"),

                "fiscal_period": fact.get("fp")

            }

            existing = selected_records.get(end)

            if (
                existing is None
                or (record["filed"] or "")
                > (existing["filed"] or "")
            ):

                selected_records[end] = record

    return selected_records


def extract_statement_periods(
    company_facts,
    statement_type,
    period_type
):

    us_gaap = (
        company_facts
        .get("facts", {})
        .get("us-gaap", {})
    )

    field_mapping = STATEMENT_FIELDS[statement_type]

    extracted = {}

    for field_name, tags in field_mapping.items():

        records = get_fact_records(
            us_gaap,
            tags,
            period_type,
            statement_type
        )

        for reporting_period, record in records.items():

            extracted.setdefault(
                reporting_period,
                {}
            )

            extracted[reporting_period][field_name] = record

    return extracted


def build_statement_records(company, company_facts):

    statement_records = []

    company_name = (
        company_facts.get("entityName")
        or company.get("company_name")
    )

    for statement_type in STATEMENT_FIELDS:

        for period_type in ("annual", "quarterly"):

            periods = extract_statement_periods(
                company_facts,
                statement_type,
                period_type
            )

            for reporting_period, data in periods.items():

                if not data:
                    continue

                fiscal_years = [
                    item["fiscal_year"]
                    for item in data.values()
                    if item.get("fiscal_year") is not None
                ]

                fiscal_quarters = [
                    item["fiscal_period"]
                    for item in data.values()
                    if item.get("fiscal_period") in (
                        "Q1", "Q2", "Q3", "Q4"
                    )
                ]

                fiscal_year = (
                    max(fiscal_years)
                    if fiscal_years
                    else None
                )

                fiscal_quarter = None

                if fiscal_quarters:

                    fiscal_quarter = int(
                        fiscal_quarters[0].replace("Q", "")
                    )

                source_document = next(
                    (
                        item.get("accession_number")
                        for item in data.values()
                        if item.get("accession_number")
                    ),
                    None
                )

                statement_records.append({

                    "ticker": company["ticker"],

                    "company_name": company_name,

                    "exchange": company.get("exchange"),

                    "country": "USA",

                    "statement_type": statement_type,

                    "reporting_period": reporting_period,

                    "period_type": period_type,

                    "fiscal_year": fiscal_year,

                    "fiscal_quarter": fiscal_quarter,

                    "currency": company.get("currency"),

                    "source": "SEC EDGAR Company Facts",

                    "source_document": source_document,

                    "data": data

                })

    return statement_records


def collect_company_statements(ticker):

    ticker = ticker.upper().strip()

    collection_id = None

    started_at = datetime.now(
        timezone.utc
    ).isoformat()

    try:

        initialise_statement_database()

        company = get_company_record(ticker)

        cik = str(
            company["sec_cik"]
        ).strip().zfill(10)

        with get_connection() as conn:

            cursor = conn.execute("""

                INSERT INTO statement_collection_history (

                    ticker,
                    exchange,
                    source,
                    status,
                    started_at

                )

                VALUES (?, ?, ?, ?, ?)

            """, (

                ticker,
                company.get("exchange"),
                "SEC EDGAR",
                "running",
                started_at

            ))

            collection_id = cursor.lastrowid

        company_facts = fetch_company_facts(cik)

        records = build_statement_records(
            company,
            company_facts
        )

        if not records:

            raise ValueError(
                "No supported annual or quarterly financial "
                "facts were found for this company."
            )

        for statement in records:

            save_financial_statement(statement)

        completed_at = datetime.now(
            timezone.utc
        ).isoformat()

        with get_connection() as conn:

            conn.execute("""

                UPDATE statement_collection_history

                SET
                    status = 'success',
                    statements_collected = ?,
                    completed_at = ?

                WHERE id = ?

            """, (

                len(records),
                completed_at,
                collection_id

            ))

        return {

            "ticker": ticker,

            "company_name": company.get("company_name"),

            "cik": cik,

            "status": "success",

            "statements_collected": len(records),

            "source": "SEC EDGAR Company Facts"

        }

    except Exception as error:

        if collection_id is not None:

            with get_connection() as conn:

                conn.execute("""

                    UPDATE statement_collection_history

                    SET
                        status = 'failed',
                        error_message = ?,
                        completed_at = ?

                    WHERE id = ?

                """, (

                    str(error),

                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                    collection_id

                ))

        return {

            "ticker": ticker,

            "status": "failed",

            "error": str(error)

        }
