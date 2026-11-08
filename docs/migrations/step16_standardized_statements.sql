-- Step 16 migration — standardized_statements table.
-- NOT applied to the production database by agents/financial_standard.py.
-- Apply only to a COPY of database/assistant.db, or via
-- create_standardized_statements_table(conn) in agents/financial_standard.py
-- on a connection the caller supplies.
--
-- One row per (raw statement row, concept). A missing concept is stored
-- as a row with present = 0 and value NULL, so "missing" is visible in
-- the table instead of silently absent. Derived concepts (total_debt,
-- free_cash_flow) are stored with derived = 1 and the derivation text.
-- Values are stored exactly as reported; no unit conversion and no
-- currency conversion is performed by Step 16.

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
