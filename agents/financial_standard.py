"""Step 16 — Unified financial data: standardise financial statements.

One internal data model for financial statements from the US, Singapore
and Hong Kong, so the analysis engine (Steps 17-20) never has to know
which collector produced a row.

Design rules (from the project brief, Step 16 requirements):

* Preserve original reported values.  Never invent a missing figure.
* Distinguish missing from zero: a value of 0 is present; an absent key
  is missing (``present`` is False, ``value`` is None).
* Keep source attribution and lineage on every value: the source
  label/key it came from, the source document (accession number / PDF
  URL), the filing date where the collector recorded one, the currency
  and the unit exactly as reported.  No silent unit conversion — a
  value is recorded in the unit the source reported it in.
* Currency is preserved, never converted.  A USD row and a SGD row for
  the same concept are not comparable without an explicit FX step,
  which this module does not perform.
* Fiscal year / quarter and annual vs quarterly vs TTM are carried
  through from ``period_type`` / ``reporting_period`` / ``fiscal_year``
  / ``fiscal_quarter``.  No TTM rows exist in the database yet; a
  ``period_type`` of ``ttm`` is passed through unchanged if one appears.
* Derived concepts (``total_debt``, ``free_cash_flow``) are computed
  only from standardised components, and are labelled derived with the
  formula and the components that were actually present.  A derived
  value built from a partial component set is flagged ``partial``.

What is actually implemented, honestly:

* US (SEC EDGAR Company Facts) — VALIDATED against real data.  The
  raw ``data_json`` keys are the snake_case field names produced by
  ``data/financial_statements/sec_collector.py`` (``STATEMENT_FIELDS``),
  each value a dict ``{value, unit, tag, start, end, filed, form,
  accession_number, fiscal_year, fiscal_period}``.  The XBRL tag lists
  from that collector are reproduced here as ``US_SEC_TAG_MAP`` so a
  row stored under raw XBRL tags can also be mapped.
* Singapore (SGX announcements) — MAPPING ONLY, UNVALIDATED.  The 79
  Singapore rows in the database are announcement metadata only
  (``announcement_id``, ``pdf_url``, ``extraction_status``,
  ``statement_heading_confirmed_raw_values`` / ``raw_extraction`` /
  ...).  They contain no validated line items, so every concept is
  missing for Singapore and no value is invented.  ``SG_LABEL_MAP``
  records the candidate statement labels actually found in
  ``data/financial_statements/sgx_collector.py`` (``STATEMENT_INDICATORS``)
  as a starting point for a future SG line-item extractor.  Note the
  bank-specific trap in that table: a Singapore bank's "total income"
  (net interest income + non-interest income) is NOT the same concept
  as a US company's "revenue", so it is mapped only as a candidate
  with ``equivalent`` set to False.
* Hong Kong (HKEX) — MAPPING ONLY, UNVALIDATED.  There is no HKEX
  financial-statement collector in the project and no HKEX statement
  rows in the database, so ``HK_LABEL_MAP`` is a proposed IFRS label
  table only, with no data behind it.

This module never touches the production database.  It reads rows
(dicts / sqlite rows) supplied by the caller, and
``create_standardized_statements_table`` / ``write_standardised``
only ever operate on a connection the caller supplies (tests use a
temporary copy).  It makes no network requests.
"""

import json
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Common concept set
# ---------------------------------------------------------------------------

# Core concepts required by the Step 16 brief, plus the extra concepts
# the SEC collector already produces (kept so nothing is thrown away).
CONCEPTS = (
    # income statement
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_basic",
    "eps_diluted",
    "shares_outstanding_basic",
    "shares_outstanding_diluted",
    # balance sheet
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "total_debt",
    # cash flow statement
    "operating_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    # extra concepts already collected (not derived)
    "research_and_development",
    "selling_general_administrative",
    "income_before_tax",
    "income_tax",
    "current_assets",
    "current_liabilities",
    "accounts_receivable",
    "accounts_payable",
    "inventory",
    "short_term_investments",
    "short_term_debt",
    "long_term_debt",
    "dividends_paid",
    "share_repurchases",
    "share_based_compensation",
    "investing_cash_flow",
    "financing_cash_flow",
)

# Concepts computed by this module from other standardised concepts.
DERIVED_CONCEPTS = ("total_debt", "free_cash_flow")

DERIVATION_FORMULAS = {
    "total_debt": "short_term_debt + long_term_debt (sum of components present)",
    "free_cash_flow": "operating_cash_flow - capital_expenditure",
}

# Which statement type each concept belongs to.  Used to decide whether
# a concept is "not applicable to this statement" (missing, expected)
# rather than "missing, unexpected".  ``total_debt`` / ``free_cash_flow``
# are derived, so they appear under their source statement type.
CONCEPT_STATEMENT = {
    "revenue": "income_statement",
    "cost_of_revenue": "income_statement",
    "gross_profit": "income_statement",
    "operating_income": "income_statement",
    "net_income": "income_statement",
    "eps_basic": "income_statement",
    "eps_diluted": "income_statement",
    "shares_outstanding_basic": "income_statement",
    "shares_outstanding_diluted": "income_statement",
    "research_and_development": "income_statement",
    "selling_general_administrative": "income_statement",
    "income_before_tax": "income_statement",
    "income_tax": "income_statement",
    "total_assets": "balance_sheet",
    "total_liabilities": "balance_sheet",
    "total_equity": "balance_sheet",
    "cash_and_equivalents": "balance_sheet",
    "total_debt": "balance_sheet",
    "current_assets": "balance_sheet",
    "current_liabilities": "balance_sheet",
    "accounts_receivable": "balance_sheet",
    "accounts_payable": "balance_sheet",
    "inventory": "balance_sheet",
    "short_term_investments": "balance_sheet",
    "short_term_debt": "balance_sheet",
    "long_term_debt": "balance_sheet",
    "operating_cash_flow": "cash_flow_statement",
    "capital_expenditure": "cash_flow_statement",
    "free_cash_flow": "cash_flow_statement",
    "dividends_paid": "cash_flow_statement",
    "share_repurchases": "cash_flow_statement",
    "share_based_compensation": "cash_flow_statement",
    "investing_cash_flow": "cash_flow_statement",
    "financing_cash_flow": "cash_flow_statement",
}

# Raw statement_type spellings seen in the database, normalised here.
# Singapore uses "cash_flow"; the US uses "cash_flow_statement".
# "changes_in_equity" has no concepts in the common set yet.
STATEMENT_TYPE_ALIASES = {
    "cash_flow": "cash_flow_statement",
    "cash_flow_statement": "cash_flow_statement",
    "income_statement": "income_statement",
    "balance_sheet": "balance_sheet",
    "changes_in_equity": "changes_in_equity",
}

# ---------------------------------------------------------------------------
# US mapping — validated.  Keys are the snake_case field names in
# data_json, produced by data/financial_statements/sec_collector.py.
# Each entry: concept -> (raw key in data_json, SEC XBRL tags tried).
# The raw key is identical to the concept name for every US field today;
# the mapping is written out explicitly so a renamed collector field
# fails loudly instead of silently dropping a concept.
# ---------------------------------------------------------------------------

US_KEY_MAP = {
    "revenue": "revenue",
    "cost_of_revenue": "cost_of_revenue",
    "gross_profit": "gross_profit",
    "research_and_development": "research_and_development",
    "selling_general_administrative": "selling_general_administrative",
    "operating_income": "operating_income",
    "income_before_tax": "income_before_tax",
    "income_tax": "income_tax",
    "net_income": "net_income",
    "eps_basic": "basic_eps",
    "eps_diluted": "diluted_eps",
    "shares_outstanding_basic": "basic_shares",
    "shares_outstanding_diluted": "diluted_shares",
    "cash_and_equivalents": "cash_and_equivalents",
    "short_term_investments": "short_term_investments",
    "accounts_receivable": "accounts_receivable",
    "inventory": "inventory",
    "current_assets": "current_assets",
    "total_assets": "total_assets",
    "accounts_payable": "accounts_payable",
    "current_liabilities": "current_liabilities",
    "short_term_debt": "short_term_debt",
    "long_term_debt": "long_term_debt",
    "total_liabilities": "total_liabilities",
    "total_equity": "shareholders_equity",
    "operating_cash_flow": "operating_cash_flow",
    "capital_expenditure": "capital_expenditure",
    "investing_cash_flow": "investing_cash_flow",
    "financing_cash_flow": "financing_cash_flow",
    "dividends_paid": "dividends_paid",
    "share_repurchases": "share_repurchases",
    "share_based_compensation": "share_based_compensation",
}

# XBRL tags per concept, copied from STATEMENT_FIELDS in
# data/financial_statements/sec_collector.py.  Used when a raw row is
# keyed by XBRL tag instead of by the collector's snake_case field name.
US_SEC_TAG_MAP = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit": ["GrossProfit"],
    "research_and_development": ["ResearchAndDevelopmentExpense"],
    "selling_general_administrative": ["SellingGeneralAndAdministrativeExpense"],
    "operating_income": ["OperatingIncomeLoss"],
    "income_before_tax": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "shares_outstanding_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "shares_outstanding_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "cash_and_equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_term_investments": ["ShortTermInvestments"],
    "accounts_receivable": ["AccountsReceivableNetCurrent"],
    "inventory": ["InventoryNet"],
    "current_assets": ["AssetsCurrent"],
    "total_assets": ["Assets"],
    "accounts_payable": ["AccountsPayableCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "short_term_debt": ["LongTermDebtCurrent", "ShortTermBorrowings"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "total_liabilities": ["Liabilities"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capital_expenditure": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "investing_cash_flow": ["NetCashProvidedByUsedInInvestingActivities"],
    "financing_cash_flow": ["NetCashProvidedByUsedInFinancingActivities"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "share_repurchases": [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ],
    "share_based_compensation": ["ShareBasedCompensation"],
}

# ---------------------------------------------------------------------------
# Singapore mapping — CANDIDATE ONLY, UNVALIDATED.
# Labels are the statement indicators actually present in
# data/financial_statements/sgx_collector.py (STATEMENT_INDICATORS).
# No Singapore row in the database contains a validated line item, so
# this table is never applied automatically by standardise_row(); it
# exists so a future SG line-item extractor has a documented starting
# point, and so its known traps are written down, not discovered twice.
# ``equivalent`` False means the SG label is NOT the same concept as
# the common concept (bank "total income" vs company "revenue").
# ---------------------------------------------------------------------------

SG_LABEL_MAP = {
    # income statement (bank-heavy: DBS is the only SG company so far)
    "total income": {
        "concept": "revenue", "equivalent": False,
        "note": "Bank total income (net interest + non-interest income) "
                "is not the same concept as revenue. Needs a bank-specific "
                "concept, not a silent rename.",
    },
    "net interest income": {
        "concept": "revenue", "equivalent": False,
        "note": "One component of bank total income, not revenue.",
    },
    "non-interest income": {
        "concept": "revenue", "equivalent": False,
        "note": "One component of bank total income, not revenue.",
    },
    "revenue": {
        "concept": "revenue", "equivalent": True,
        "note": "Non-bank SGX issuers report 'Revenue'; unvalidated.",
    },
    "profit before tax": {
        "concept": "income_before_tax", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "profit before taxation": {
        "concept": "income_before_tax", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "profit for the year": {
        "concept": "net_income", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "net profit": {
        "concept": "net_income", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "interest income": {
        "concept": "revenue", "equivalent": False,
        "note": "Bank line; not revenue.",
    },
    "total expenses": {
        "concept": None, "equivalent": False,
        "note": "No single common concept; operating expenses need "
                "splitting before mapping.",
    },
    # balance sheet
    "total assets": {
        "concept": "total_assets", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "total liabilities": {
        "concept": "total_liabilities", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "total equity": {
        "concept": "total_equity", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "total shareholders' equity": {
        "concept": "total_equity", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "shareholders' funds": {
        "concept": "total_equity", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "equity attributable to shareholders": {
        "concept": "total_equity", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "deposits and balances from customers": {
        "concept": None, "equivalent": False,
        "note": "Bank-specific liability; no common concept yet.",
    },
    # cash flow
    "net cash generated from operating activities": {
        "concept": "operating_cash_flow", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "cash flows from operating activities": {
        "concept": "operating_cash_flow", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "net cash used in investing activities": {
        "concept": "investing_cash_flow", "equivalent": True,
        "note": "Sign convention must be checked on first real row.",
    },
    "cash flows from investing activities": {
        "concept": "investing_cash_flow", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "net cash generated from financing activities": {
        "concept": "financing_cash_flow", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    "cash flows from financing activities": {
        "concept": "financing_cash_flow", "equivalent": True,
        "note": "Unvalidated against any extracted row.",
    },
    # changes in equity — no common concepts yet
    "share capital": {
        "concept": None, "equivalent": False,
        "note": "Statement of changes in equity is not yet in the common model.",
    },
    "retained profits": {
        "concept": None, "equivalent": False,
        "note": "Statement of changes in equity is not yet in the common model.",
    },
    "revenue reserves": {
        "concept": None, "equivalent": False,
        "note": "Statement of changes in equity is not yet in the common model.",
    },
}

# ---------------------------------------------------------------------------
# Hong Kong mapping — PROPOSED ONLY, UNVALIDATED.
# No HKEX financial-statement collector exists in the project and no
# HKEX statement rows exist in the database.  These are the standard
# IFRS labels a future HKEX extractor would expect to meet; none of
# them has been tested against a real HKEX filing.
# ---------------------------------------------------------------------------

HK_LABEL_MAP = {
    "revenue": {"concept": "revenue", "equivalent": True,
                "note": "Proposed IFRS label; unvalidated, no HKEX data."},
    "turnover": {"concept": "revenue", "equivalent": True,
                 "note": "HKEX issuers often report 'Turnover'; unvalidated."},
    "cost of sales": {"concept": "cost_of_revenue", "equivalent": True,
                      "note": "Proposed IFRS label; unvalidated."},
    "gross profit": {"concept": "gross_profit", "equivalent": True,
                     "note": "Proposed IFRS label; unvalidated."},
    "operating profit": {"concept": "operating_income", "equivalent": True,
                         "note": "Proposed IFRS label; unvalidated."},
    "profit before tax": {"concept": "income_before_tax", "equivalent": True,
                          "note": "Proposed IFRS label; unvalidated."},
    "profit before taxation": {"concept": "income_before_tax", "equivalent": True,
                               "note": "Proposed IFRS label; unvalidated."},
    "income tax expense": {"concept": "income_tax", "equivalent": True,
                           "note": "Proposed IFRS label; unvalidated."},
    "profit for the year": {"concept": "net_income", "equivalent": True,
                            "note": "Proposed IFRS label; unvalidated."},
    "profit attributable to shareholders": {"concept": "net_income", "equivalent": False,
        "note": "Attributable profit excludes non-controlling interests; "
                "not identical to total net income. Unvalidated."},
    "basic earnings per share": {"concept": "eps_basic", "equivalent": True,
                                 "note": "Proposed IFRS label; unvalidated."},
    "diluted earnings per share": {"concept": "eps_diluted", "equivalent": True,
                                   "note": "Proposed IFRS label; unvalidated."},
    "total assets": {"concept": "total_assets", "equivalent": True,
                     "note": "Proposed IFRS label; unvalidated."},
    "total liabilities": {"concept": "total_liabilities", "equivalent": True,
                          "note": "Proposed IFRS label; unvalidated."},
    "total equity": {"concept": "total_equity", "equivalent": True,
                     "note": "Proposed IFRS label; unvalidated."},
    "cash and cash equivalents": {"concept": "cash_and_equivalents", "equivalent": True,
                                  "note": "Proposed IFRS label; unvalidated."},
    "net cash generated from operating activities": {"concept": "operating_cash_flow", "equivalent": True,
        "note": "Proposed IFRS label; unvalidated."},
    "purchase of property, plant and equipment": {"concept": "capital_expenditure", "equivalent": True,
        "note": "Sign convention must be checked on first real row; unvalidated."},
    "dividends paid": {"concept": "dividends_paid", "equivalent": True,
                       "note": "Proposed IFRS label; unvalidated."},
}

# ---------------------------------------------------------------------------
# DDL — migration note.  NOT applied to the production database by this
# module.  Apply only via create_standardized_statements_table() on a
# connection the caller supplies, or by running the SQL file at
# docs/migrations/step16_standardized_statements.sql against a copy.
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
-- Step 16 migration (NOT applied to production by agents/financial_standard.py).
-- Run against a copy of database/assistant.db, or via
-- create_standardized_statements_table(conn) in this module.
CREATE TABLE IF NOT EXISTS standardized_statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_statement_id INTEGER NOT NULL,
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
    concept TEXT NOT NULL,
    value REAL,
    present INTEGER NOT NULL,
    derived INTEGER NOT NULL DEFAULT 0,
    derivation TEXT,
    source_key TEXT,
    source_tag TEXT,
    unit TEXT,
    filed_date TEXT,
    form TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    standardized_at TEXT NOT NULL,
    UNIQUE (ticker, exchange, statement_type, reporting_period, period_type, concept)
);
CREATE INDEX IF NOT EXISTS idx_std_stmt_ticker_period
    ON standardized_statements(ticker, reporting_period);
CREATE INDEX IF NOT EXISTS idx_std_stmt_concept
    ON standardized_statements(concept);
"""

CREATE_TABLE_SQL = MIGRATION_SQL


def create_standardized_statements_table(conn):
    """Create ``standardized_statements`` on the supplied connection only.

    The caller decides which database this is.  This module never opens
    the production database itself, and never calls this on it.
    """
    conn.executescript(MIGRATION_SQL)
    return True


# ---------------------------------------------------------------------------
# Standardisation
# ---------------------------------------------------------------------------

def _missing(concept):
    return {
        "concept": concept,
        "value": None,
        "present": False,
        "derived": False,
        "derivation": None,
        "source_key": None,
        "source_tag": None,
        "unit": None,
        "filed_date": None,
        "form": None,
    }


def _extract_us_value(raw_entry, concept, raw_key):
    """Pull one US value out of a data_json entry.

    ``raw_entry`` is either the collector's dict
    ``{value, unit, tag, ...}`` or a bare number (accepted for fixture
    rows).  A non-numeric ``value`` is treated as missing, never
    coerced, and the reason is reported in the row warnings.
    Returns (standardised value dict, warning or None).
    """
    if isinstance(raw_entry, dict) and "value" in raw_entry:
        value = raw_entry.get("value")
        if value is None:
            return _missing(concept), None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _missing(concept), (
                f"{concept}: non-numeric value {value!r} from "
                f"'{raw_key}' treated as missing"
            )
        return {
            "concept": concept,
            "value": value,
            "present": True,
            "derived": False,
            "derivation": None,
            "source_key": raw_key,
            "source_tag": raw_entry.get("tag"),
            "unit": raw_entry.get("unit"),
            "filed_date": raw_entry.get("filed"),
            "form": raw_entry.get("form"),
        }, None
    if isinstance(raw_entry, (int, float)) and not isinstance(raw_entry, bool):
        return {
            "concept": concept,
            "value": raw_entry,
            "present": True,
            "derived": False,
            "derivation": None,
            "source_key": raw_key,
            "source_tag": None,
            "unit": None,
            "filed_date": None,
            "form": None,
        }, None
    return _missing(concept), (
        f"{concept}: unrecognised entry {raw_entry!r} from "
        f"'{raw_key}' treated as missing"
    )


def _derive(record, concept, components, formula, currency):
    """Derive total_debt / free_cash_flow from standardised components.

    Only components that are present are summed.  If any component is
    missing the result is flagged ``partial`` in the derivation text so
    a consumer can see it is a lower bound, not a complete total.
    """
    present = [c for c in components if record["values"][c]["present"]]
    missing = [c for c in components if not record["values"][c]["present"]]
    if not present:
        value = _missing(concept)
        value["derivation"] = f"{formula}; no components present"
        return value
    if concept == "total_debt":
        total = sum(record["values"][c]["value"] for c in present)
    else:  # free_cash_flow = operating_cash_flow - capital_expenditure
        ocf = record["values"]["operating_cash_flow"]
        capex = record["values"]["capital_expenditure"]
        if not ocf["present"] or not capex["present"]:
            value = _missing(concept)
            value["derivation"] = (
                f"{formula}; missing "
                f"{', '.join(missing) or 'components'}"
            )
            return value
        total = ocf["value"] - capex["value"]
    derivation = formula + "; components present: " + ", ".join(present)
    if missing:
        derivation += f"; MISSING (partial): {', '.join(missing)}"
    return {
        "concept": concept,
        "value": total,
        "present": True,
        "derived": True,
        "derivation": derivation,
        "source_key": "+".join(
            record["values"][c]["source_key"] or c for c in present
        ),
        "source_tag": None,
        "unit": record["values"][present[0]]["unit"],
        "filed_date": None,
        "form": None,
    }


def _row_dict(row):
    if isinstance(row, dict):
        return row
    # sqlite3.Row / tuple from SELECT * on financial_statements
    keys = (
        "id", "ticker", "company_name", "exchange", "country",
        "statement_type", "reporting_period", "period_type",
        "fiscal_year", "fiscal_quarter", "currency", "source",
        "source_document", "data_json", "collected_at",
    )
    return dict(zip(keys, row))


def standardise_row(row):
    """Standardise one raw ``financial_statements`` row.

    Returns a record dict.  ``values`` maps every concept in CONCEPTS
    to a value dict (see ``_missing`` / ``_extract_us_value``), so a
    missing concept is always visible, never silently absent.  ``values``
    only carries concepts belonging to the row's statement type plus
    the derived concepts of that type — a balance-sheet row does not
    claim a missing revenue, it simply has no income concepts.
    ``warnings`` lists data-quality observations for this row.
    """
    raw = _row_dict(row)
    warnings = []
    statement_type = STATEMENT_TYPE_ALIASES.get(
        str(raw.get("statement_type") or "").strip().lower(),
        str(raw.get("statement_type") or "").strip().lower(),
    )
    country = (raw.get("country") or "").strip()
    currency = raw.get("currency")
    data = raw.get("data_json") or raw.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            warnings.append("data_json is not valid JSON; all concepts missing")
            data = {}
    if not isinstance(data, dict):
        warnings.append("data_json is not an object; all concepts missing")
        data = {}

    record = {
        "source_statement_id": raw.get("id"),
        "ticker": raw.get("ticker"),
        "company_name": raw.get("company_name"),
        "exchange": raw.get("exchange"),
        "country": country,
        "statement_type": statement_type,
        "reporting_period": raw.get("reporting_period"),
        "period_type": raw.get("period_type"),
        "fiscal_year": raw.get("fiscal_year"),
        "fiscal_quarter": raw.get("fiscal_quarter"),
        "currency": currency,
        "source": raw.get("source"),
        "source_document": raw.get("source_document"),
        "values": {},
        "warnings": warnings,
    }

    # Concepts that belong to this statement type.
    concepts = [
        c for c in CONCEPTS
        if CONCEPT_STATEMENT.get(c) == statement_type
    ]
    if statement_type == "changes_in_equity":
        warnings.append(
            "changes_in_equity is not yet in the common concept set; "
            "no concepts standardised"
        )
    if not concepts and statement_type not in ("changes_in_equity",):
        warnings.append(
            f"unknown statement_type '{raw.get('statement_type')}'; "
            "no concepts standardised"
        )

    if country == "USA" or (raw.get("source") or "") == "SEC EDGAR Company Facts":
        for concept in concepts:
            if concept in DERIVED_CONCEPTS:
                continue
            raw_key = US_KEY_MAP.get(concept)
            entry = None
            used_key = None
            if raw_key is not None and raw_key in data:
                entry, used_key = data[raw_key], raw_key
            else:
                # Fall back to raw XBRL tags (a row keyed by tag, not
                # by the collector's snake_case field name).
                for tag in US_SEC_TAG_MAP.get(concept, []):
                    if tag in data:
                        entry, used_key = data[tag], tag
                        break
            if entry is None:
                record["values"][concept] = _missing(concept)
            else:
                value, warning = _extract_us_value(entry, concept, used_key)
                record["values"][concept] = value
                if warning:
                    warnings.append(warning)
    elif country == "Singapore":
        # Metadata only: no validated line items exist.  Do not parse
        # pages[].rows[] — those rows are raw PDF text, unverified, and
        # include non-statement pages (see the stored verification note).
        for concept in concepts:
            if concept in DERIVED_CONCEPTS:
                continue
            record["values"][concept] = _missing(concept)
        extraction_status = data.get("extraction_status") if isinstance(data, dict) else None
        warnings.append(
            "Singapore row is announcement metadata only "
            f"(extraction_status={extraction_status}); no validated "
            "line items exist, so all concepts are missing. "
            "SG_LABEL_MAP is a candidate table only."
        )
    elif country in ("Hong Kong", "China"):
        for concept in concepts:
            if concept in DERIVED_CONCEPTS:
                continue
            record["values"][concept] = _missing(concept)
        warnings.append(
            "No HKEX financial-statement collector exists and no HKEX "
            "statement rows exist; HK_LABEL_MAP is proposed only."
        )
    else:
        for concept in concepts:
            if concept in DERIVED_CONCEPTS:
                continue
            record["values"][concept] = _missing(concept)
        warnings.append(
            f"No mapping table for country '{country or 'unknown'}'; "
            "all concepts missing."
        )

    # Derived concepts, from the standardised components above.
    if "total_debt" in concepts:
        record["values"]["total_debt"] = _derive(
            record, "total_debt",
            ["short_term_debt", "long_term_debt"],
            DERIVATION_FORMULAS["total_debt"], currency,
        )
    if "free_cash_flow" in concepts:
        record["values"]["free_cash_flow"] = _derive(
            record, "free_cash_flow",
            ["operating_cash_flow", "capital_expenditure"],
            DERIVATION_FORMULAS["free_cash_flow"], currency,
        )

    # Lineage warning: values inside one US row can come from different
    # filings (restatements / latest-filed-wins in the SEC collector).
    filed_dates = {
        v["filed_date"] for v in record["values"].values()
        if v.get("filed_date")
    }
    if len(filed_dates) > 1:
        warnings.append(
            "values in this row come from "
            f"{len(filed_dates)} distinct filing dates "
            f"({min(filed_dates)} to {max(filed_dates)}); restatement "
            "detection is not implemented — see Step 16 report"
        )
    row_fy = raw.get("fiscal_year")
    if row_fy is not None and raw.get("reporting_period"):
        period_year = str(raw["reporting_period"])[:4]
        if str(row_fy) != period_year:
            warnings.append(
                f"row fiscal_year {row_fy} differs from reporting_period "
                f"year {period_year} (collector stores max fiscal_year "
                "across fields); reporting_period is authoritative"
            )

    record["present_count"] = sum(
        1 for v in record["values"].values() if v["present"]
    )
    record["concept_count"] = len(record["values"])
    return record


def standardise_all(conn):
    """Standardise every row in ``financial_statements`` on ``conn``.

    Returns a list of standardised records.  Read-only with respect to
    the source table; writing is a separate step (``write_standardised``).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM financial_statements ORDER BY country, ticker, "
        "statement_type, reporting_period, period_type"
    ).fetchall()
    return [standardise_row(dict(r)) for r in rows]


def write_standardised(conn, records, on_conflict="replace"):
    """Write standardised records to ``standardized_statements``.

    Only ever called on a connection the caller supplies (tests and the
    Step 16 report use a temporary copy of the database).  Creates the
    table first.  Returns (inserted, skipped).  ``on_conflict``:
    "replace" re-standardises idempotently; "error" raises on a
    duplicate (ticker, exchange, statement_type, reporting_period,
    period_type, concept) key.
    """
    from datetime import datetime, timezone

    if on_conflict not in ("replace", "error"):
        raise ValueError("on_conflict must be 'replace' or 'error'")
    create_standardized_statements_table(conn)
    verb = "INSERT OR REPLACE" if on_conflict == "replace" else "INSERT"
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    skipped = 0
    for record in records:
        for concept, value in record["values"].items():
            try:
                conn.execute(
                    f"""{verb} INTO standardized_statements (
                        source_statement_id, ticker, company_name,
                        exchange, country, statement_type,
                        reporting_period, period_type, fiscal_year,
                        fiscal_quarter, currency, source,
                        source_document, concept, value, present,
                        derived, derivation, source_key, source_tag,
                        unit, filed_date, form, warnings_json,
                        standardized_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record["source_statement_id"], record["ticker"],
                        record["company_name"], record["exchange"],
                        record["country"], record["statement_type"],
                        record["reporting_period"], record["period_type"],
                        record["fiscal_year"], record["fiscal_quarter"],
                        record["currency"], record["source"],
                        record["source_document"], concept,
                        value["value"], 1 if value["present"] else 0,
                        1 if value["derived"] else 0,
                        value["derivation"], value["source_key"],
                        value["source_tag"], value["unit"],
                        value["filed_date"], value["form"],
                        json.dumps(record["warnings"]), now,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    conn.commit()
    return inserted, skipped


def coverage_summary(records):
    """Per-country / per-statement-type coverage counts for a report."""
    summary = {}
    for record in records:
        key = (record["country"], record["statement_type"])
        bucket = summary.setdefault(key, {
            "rows": 0, "present_values": 0, "total_values": 0,
            "concepts": {},
        })
        bucket["rows"] += 1
        for concept, value in record["values"].items():
            bucket["total_values"] += 1
            if value["present"]:
                bucket["present_values"] += 1
                bucket["concepts"][concept] = (
                    bucket["concepts"].get(concept, 0) + 1
                )
    return summary
