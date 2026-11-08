# Jarvis Project Status

## Current development position
- Current step: 31 — Market news collection (PARTIAL — built and tested 2026-11-08; live MSFT collection ran, 5 real dated articles + 5 hub pages from the text-search probe — see Step 31 section below)
- Current subtask: Step 32 daily market summaries
- Overall status: In progress

## Completed work

- Steps 1–6 are marked DONE in `AGENTS.md`; project evidence includes `main.py`, the `agent/` package, and the SQLite database.
- Step 9 is marked IMPLEMENTED; finance, research, calculator, and valuation tools and corresponding agent modules are present.
- Steps 11A and 11B are marked DONE; the shared financial statement database and SEC/SGX collector modules are present.
- Step 12 is marked IMPLEMENTED; `agents/market_universe.py` exists and the production database contains a `market_universe` table.
- Step 14 discovery/normalization work through 14G is marked DONE or completed in `AGENTS.md`; HKEX workbook, normalized candidates, validation flags, and review outputs are present. 14H decisions and 14I import remain pending.
- Steps 15A–15E are marked DONE in `AGENTS.md`. Database inspection confirmed 7,625 US listing rows and 6,040 distinct CIKs.
- Step 15F — US SEC issuer enrichment is PILOT-VALIDATED for the isolated 20-CIK run, but is not fully production-approved. The module validates cache entries, handles refresh timestamps and retry behavior, journals per-CIK progress, creates cache recovery backups before replacement, and produces coverage and CSV outputs.
- A reusable `--preflight-only` command is implemented in `agents/us_issuer_enrichment.py`. It uses read-only SQLite access and does not call SEC or write files.
- Step 15G candidate-classification pipeline is implemented in `agents/us_security_classifier.py`. It reads US listings through SQLite URI `mode=ro`, classifies every listing independently, accepts exact listing evidence from a local evidence CSV, retains special-security candidates, and routes absent/conflicting primary evidence to manual review. It only writes candidate CSVs and a report; it does not change the database schema or import listings.
- Step 15G Phase 2 collectors are implemented in `agents/us_security_evidence.py` for Nasdaq Trader's combined symbol directory and NYSE's public Listings Directory categories. Raw snapshots are content-hash named, timestamped, source-attributed and reused from a 24-hour cache; requests are rate-limited, retry-aware, and honor `Retry-After`.

## Current work

Step 15F completed an isolated 20-CIK pilot on 2026-11-08: all 20 SEC fetches succeeded, coverage was 20 matched with no failures, and production database/cache hashes remained unchanged. The pilot does not authorize full-universe enrichment. Known limits: stale-cache refresh behavior was not exercised because the isolated pilot cache began empty; three issuers have no SIC information; Pembina Pipeline has additional SEC-associated tickers requiring security-level classification.

Step 15G Phase 2 generated exchange-backed evidence in v3. Phase 3 has now corrected case-sensitive identity and parser artifacts and generated v4_2 from the same cached snapshots. Results are 137 confirmed, 6,116 provisional, 37 conflicting, and 1,335 unknown listings. All nonconfirmed listings remain in manual review; production import and database schema changes remain gated.

## Pending work

- Review the Phase 2 candidates and collect additional SEC filing or issuer-document evidence for unresolved classes and issuer structures; do not treat name/ticker hints or secondary sources as confirmation.
- Keep Step 15H validation/normalization and Step 15I production import separately gated; no production import or schema change has occurred.
- Assess the remaining issuer-structure gap before deciding whether to collect targeted official SEC evidence. Phase 3 did not collect data or run SEC requests.

### 2026-11-08 — Step 15G Phase 3 review decision (Muse review of Codex completion report, commits 8f6c7b9 + 1b6a6e1)

- Acceptance criteria checked: v3 outputs preserved untouched; new results in fresh versioned dirs (v4_2 is final, v4/v4_1 marked superseded); 20 tests pass in a clean venv with python-dotenv installed (17 classifier/evidence/phase-3 + 3 pilot); before/after counts reported (confirmed 137 -> 137, provisional 6,120 -> 6,116, conflicting 8 -> 37, unknown 1,360 -> 1,335; 196 material changes, each with old/new values, source and rationale in v3_to_phase3_classification_changes.csv); production DB hash unchanged (812ECB53…89D4CD); 0 network requests in Phase 3 (hash-verified cached snapshots only).
- Phase 3A done: BSBR, RNP, TPC, VBF, WDH no longer conflict (all provisional, form known, structure unknown); IPB, MHNC, TVC remain conflicting in manual review, as specified.
- Phase 3B done: MIC reconciliation view built (4,582 rows: XASE 327, ARCX 2,752, XNGS 1,503; review-only, not written into market_universe); explicit description parsing moved 34 unknown -> provisional and 9 provisional -> unknown, and surfaced 34 new genuine Nasdaq/NYSE disagreements (now conflicting, not guessed).
- Decision: **15G is sufficient to move on.** Do not run another classification round now. Security form is known for 6,239 of 7,625 listings; issuer structure remains unknown for 7,473 — that gap is documented and does not block Step 16 (financial statements are issuer-level, keyed by CIK). Unknown/conflicting rows stay separated from supported instruments; they are not deleted.
- Workspace synced to origin/main 1b6a6e1 on 2026-11-08; Muse's earlier heuristic classifier (common 6,499 / manual 110) is superseded by the pushed evidence-based classifier and is not to be merged over it.
- Hygiene fixed in workspace (unpushed, pending GitHub auth): `.env` removed from tracking, `.env.example` added with a neutral placeholder, `requirements.txt` populated (python-dotenv, requests, openpyxl, apscheduler, ddgs — was 0 bytes). Note: removing `.env` from tracking does NOT remove the personal email from earlier Git history; that needs a history rewrite or accepting it stays in old commits.
- Next: Phase A close-out needs owner decisions — HKEX import (2,793 candidates, country 'Hong Kong') and US 15J import scope — then Step 16 unified financial data.

### 2026-11-08 — Step 16 unified financial data (PARTIAL, not complete)

- Built `agents/financial_standard.py`: a standardiser over the 34-concept
  common set (revenue, cost_of_revenue, gross_profit, operating_income,
  net_income, total_assets, total_liabilities, total_equity,
  cash_and_equivalents, total_debt, operating_cash_flow,
  capital_expenditure, free_cash_flow, shares/EPS, plus the extra concepts
  the SEC collector already produces). Every standardised value carries
  source key, XBRL tag, unit and currency as reported, filing date/form,
  and a present/missing flag. Missing is stored, never silently absent;
  0 is present. No unit conversion, no currency conversion.
- Derived concepts only, labelled as derived: `total_debt =
  short_term_debt + long_term_debt` (flagged partial if a component is
  missing) and `free_cash_flow = operating_cash_flow - capital_expenditure`.
- Migration (NOT applied to production): `docs/migrations/
  step16_standardized_statements.sql` and `MIGRATION_SQL` /
  `create_standardized_statements_table(conn)` in the module, which only
  ever operates on a caller-supplied connection. Table
  `standardized_statements`, one row per (statement, concept), unique key
  (ticker, exchange, statement_type, reporting_period, period_type, concept).
- Tests: `test_financial_standard.py` — **21 tests, OK**
  (`python -m unittest test_financial_standard`). Fixture rows + temp DB
  copies only; no network requests, no production writes. Covers US
  income/balance/cash-flow mapping, XBRL-tag fallback, missing-vs-zero,
  currency preserved, SG metadata-only rows inventing nothing, duplicate /
  unique-key behaviour (re-run idempotent, `on_conflict="error"` skips).
- Run against a temp copy of the real DB (321 rows in, 321 standardised,
  3,460 `standardized_statements` rows written, 0 skipped; production
  SHA-256 verified unchanged before/after). Full numbers in
  `step16-report.md`. Coverage: USA income 879/962 (91.4%), balance
  1,091/1,222 (89.3%), cash flow 519/592 (87.7%); Singapore 0% on all
  statement types; total 2,489/3,460 (71.9%).
- US data is MSFT only (242 rows, 2007-06-30 to 2026-09-30). Gross profit
  present 65/74, short-term investments 43/94, operating cash flow 62/74,
  share-based compensation 49/74; total_debt derived 87/94 (3 partial),
  free_cash_flow derived 62/74.
- Singapore (DBS/D05, 79 rows) is 0% by design: rows are announcement
  metadata only (extraction_status `statement_heading_confirmed_raw_values`
  / `raw_extraction` / heading-verified; unit "Not yet standardised") with
  no validated line items. Raw `pages[].rows[]` PDF text was deliberately
  NOT parsed (unverified, includes non-statement pages). `SG_LABEL_MAP`
  is a candidate table only, from the SGX collector's
  `STATEMENT_INDICATORS`; it flags that bank "total income" is not revenue.
- Hong Kong: no statement collector, no HKEX statement rows, no HKEX
  rows in `market_universe`. `HK_LABEL_MAP` is a proposed IFRS label
  table only, unvalidated.
- Data-quality findings recorded in `step16-report.md`: (1) row
  `fiscal_year` differs from the reporting-period year in 236/242 US rows
  (collector stores max fiscal_year across fields; reporting_period is
  authoritative); (2) 87 US rows mix up to 4 distinct filing dates —
  restatement detection is NOT implemented; (3) early US balance sheets
  (2007–2009) are sparse; (4) Singapore years appear twice (12-30 and
  12-31, same annual report); (5) `changes_in_equity` (21 SG rows) has no
  concepts in the model yet; (6) no TTM rows exist.
- Remaining incomplete: SG line-item extraction (needs a validated SG
  extractor; AGENTS.md 11D is PAUSED / REDESIGN REQUIRED), HKEX
  statements (needs 14I import + a new collector), restatement
  detection, US coverage beyond MSFT. **Step 16 is PARTIAL, not
  complete.** It is sufficient to start Step 17 (ratios) on US data.

### 2026-11-08 — Step 17 financial ratios and metrics engine (PARTIAL, not complete)

- Built `agents/financial_ratios.py`: computes P/E, P/B, P/S,
  EV/EBITDA, EV/EBIT, gross / operating / net margin, ROE, ROA,
  ROIC, debt-to-equity, interest coverage, current ratio and quick
  ratio from Step 16 standardised statements plus an explicit
  price/market-cap input. Every ratio records formula, inputs used,
  period, period type and price as-of date. Missing inputs are
  missing with a reason; zero denominators are missing, never an
  exception. No network requests, no production writes.
- Definitions: ROE/ROA/ROIC use average balances when a prior
  period exists (basis recorded); ROIC = NOPAT / invested capital,
  NOPAT = operating_income x (1 - income_tax / income_before_tax),
  invested capital = total_debt + total_equity -
  cash_and_equivalents; EV = market_cap + total_debt -
  cash_and_equivalents (short-term investments not subtracted);
  quick ratio = (current_assets - inventory) / current_liabilities.
- TTM: no TTM rows exist and no quarterly row is stored at a fiscal
  year end, so TTM flows are built as annual + latest quarter -
  same quarter a year ago (balance concepts from the latest
  quarterly balance row); a four-quarter sum is used only when the
  four quarters are actually consecutive (a gap forces the
  annual-minus-prior-quarter method — caught by a test).
- Tests: `test_financial_ratios.py` — **21 tests, OK**
  (`python -m unittest test_financial_ratios`). Covers MSFT FY2026
  against hand-computed values, FY2025 fundamentals, missing-input
  and zero-denominator behaviour, TTM construction, quarterly mode,
  and snapshot-price loading from a temp DB copy.
- Run against a temp copy of the real DB (production SHA-256
  unchanged). MSFT FY2026 (price as of 2026-11-03, $507.12):
  P/E 28.25, P/B 8.54, P/S 11.35, EV/EBIT 24.38, gross margin
  67.94%, operating margin 46.78%, net margin 40.31%, ROE 34.04%,
  ROA 19.42%, ROIC 30.59%, debt-to-equity 0.09, current 1.23,
  quick 1.22. FY2025 fundamentals: gross 68.82%, net 36.15%,
  ROE 33.28%, ROA 18.00%, ROIC 32.50%, D/E 0.13, current 1.35.
  TTM to 2026-09-30: P/E 26.47 (matches the snapshot's stored
  trailing_pe 26.46764, corroborating the TTM construction),
  P/S 10.87, EV/EBIT 23.17, net margin 41.17%, ROIC 30.37%.
  19 annual periods computed (FY2008–FY2026); early years degrade
  gracefully (FY2008 yields only 4 ratios — sparse balance sheets).
  Full table in `step17-report.md`.
- Unavailable, not guessed: **EV/EBITDA** (no D&A or EBITDA
  concept in Step 16, no such key in US raw data) and **interest
  coverage** (no interest expense concept or key). **Step 17 is
  PARTIAL, not complete.** Price ratios rest on a single stale
  2026-11-03 snapshot (earlier years get fundamental ratios only);
  only MSFT has statements; no SG/HK ratios exist. EV/EBITDA and
  interest coverage need D&A and interest expense added to the
  Step 16 concept set (SEC XBRL tags exist for both). Sufficient
  to start Step 18 (financial health) on US data.

### 2026-11-08 — Step 18 financial health and quality analysis (PARTIAL, not complete)

- Built `agents/financial_health.py`: structured health assessment
  per ticker/period reusing Step 16 standardised values and Step 17
  ratios (nothing re-derived by a different definition). Sections:
  (a) balance-sheet strength, (b) liquidity, (c) solvency / debt
  sustainability, (d) profitability consistency over 5y and 10y
  windows (mean/min/max/CV, share of profitable years), (e) distress
  screen (Altman components only where inputs exist, plus hard
  distress flags). Overall verdict `strong` / `adequate` / `weak` /
  `insufficient data`, always with reasons listed; thresholds are
  documented judgement rules in the module.
- Tests: `test_financial_health.py` — **11 tests, OK** (53 total with
  Steps 16–17, `python -m unittest test_financial_health
  test_financial_ratios test_financial_standard`). Covers MSFT FY2026
  and FY2025 against hand-checked values, sparse-FY2008 graceful
  degradation, fixture-driven `weak` verdict, missing-input
  behaviour, and consistency metrics. Two test bugs fixed before
  delivery ($m vs $ units; fixture used `total_equity` where the
  real raw key is `shareholders_equity`).
- Run against a temp copy of the real DB (production SHA-256
  unchanged, 812ECB53…89D4CD). MSFT FY2026 verdict: **STRONG** —
  equity ratio 58.33%, debt-to-equity 0.09, net debt $19,359m, cash
  cover 0.52, current 1.23, quick 1.22, working capital $38,885m,
  debt / operating cash flow 0.22x, debt / FCF 0.60x, FCF after
  dividends $40,542m, debt down 31% over 5 years, 5y net-margin
  mean 36.6% (CV 0.062), 5y mean ROE 38.1%, 5y mean ROIC 33.9%,
  5/5 profitable years. Watch item: working capital down 51% over
  3 years and 59% over 5 years (current liabilities growing faster
  than current assets) while the current ratio stayed above 1.
  Verdicts over 19 annual periods: FY2008 insufficient data, FY2009–
  FY2011 adequate, FY2012–FY2013 strong, FY2014–FY2022 adequate
  (5y windows include the FY2018 tax-charge year: net margin 15.0%,
  10y CV 0.223), FY2023–FY2026 strong. No year was `weak`.
- Unavailable, not guessed: **Altman Z-score** (retained earnings
  is not a Step 16 concept and no such key exists in the US raw
  data — 4 of 5 components exist, including market value of equity
  / liabilities 11.92 on the stale 2026-11-03 price); **net debt /
  EBITDA** and **interest coverage** (same Step 16 gaps as Step 17).
  Adding retained earnings (SEC tag
  `RetainedEarningsAccumulatedDeficit`) plus D&A and interest
  expense to the Step 16 concept set would unlock all three.
  **Step 18 is PARTIAL, not complete.** MSFT only; verdict
  thresholds are uncalibrated judgement rules (no distressed issuer
  in the DB to test against — `weak` is exercised only by fixture
  rows); working-capital decline is reported, not scored; no TTM
  health assessment yet. Sufficient to start Steps 19–20 on US data.

### 2026-11-08 — Step 19 growth analysis (PARTIAL, not complete)

- Built `agents/financial_growth.py`: YoY growth, 3y/5y/10y CAGRs,
  growth consistency and a cyclicality assessment for revenue,
  operating income, net income, diluted EPS, operating cash flow
  and free cash flow, reusing Step 16 standardised values and Step
  16's derived free cash flow; no price used anywhere. YoY is
  missing (not nonsense) when the prior value is zero or negative;
  CAGR is undefined when the start value is ≤ 0 or the end value
  is < 0 (end value of exactly 0 is −100%); missing endpoints are
  never interpolated. Quarterly YoY compares a quarter with the
  same quarter a year ago. Cyclicality verdicts (steady / moderate
  / volatile) come from YoY volatility, declines in the last 10y
  and the largest peak-to-trough level drawdown; thresholds are
  documented judgement rules, uncalibrated (no peer set in the DB).
- Tests: `test_financial_growth.py` — **35 tests, OK** (88 total
  with Steps 16–18, no regressions). Covers YoY edge cases
  (zero/negative prior, missing values), CAGR edge cases (negative
  start/end, zero end, insufficient history, missing endpoint),
  consistency, drawdowns, cyclicality verdicts, quarterly YoY,
  and hand-checked MSFT values on a temp DB copy.
- MSFT FY2026 YoY: revenue +17.8%, operating income +20.8%, net
  income +31.3%, diluted EPS +31.6%, operating cash flow +34.4%,
  free cash flow −6.5% (capex rose faster than OCF). 10y CAGRs:
  revenue +13.8%, operating income +19.5%, net income +20.6%, EPS
  +21.5%, OCF +18.6%, FCF +10.4%. 5y CAGRs: revenue +14.6%, net
  income +16.9%, EPS +17.4%, FCF only +3.6%. Revenue grew in 16
  of 18 years and is "steady"; earnings are "volatile" — driven by
  two identified one-offs (FY2015 Nokia write-down, FY2018 tax
  charge), not a repeating cycle. Largest net-income drawdown
  47.3% (2011 peak → 2015 trough). Latest quarter YoY
  (2026-09-30): revenue +18.8%, net income +32.0%, FCF −27.4%.
- OCF/FCF YoY are missing for FY2014–FY2016 (sparse early
  cash-flow rows, Step 16 finding) — excluded from consistency
  counts, not treated as zero. TTM growth vs the prior TTM is not
  computed (no prior-year TTM construction available); quarterly
  YoY is the like-for-like alternative. No SG/HK growth (Step 16
  standardised 0 concepts for DBS, none for HKEX).
- **Step 19 is PARTIAL, not complete.** MSFT only; thresholds
  uncalibrated against peers or a genuinely cyclical company.
  Sufficient to start Step 20 (cash flow / owner earnings) on US
  data; growth figures feed Steps 21 (valuation) and 24 (business
  quality).

### 2026-11-08 — Phase A close-out: HKEX import (14I) and US import (15J)

- Owner approved both imports on 2026-11-08 ("finish it"). US scope
  decided as core equity-like only; specials and manual-review rows
  wait for 15I review.
- 14I: `agents/hkex_import.py` first failed on a missing
  `last_updated` (NOT NULL) in the INSERT — fixed to stamp the
  import time; the failed run wrote nothing (DB hash unchanged).
  Applied: **2,793 inserted, 0 skipped** (2,782 equity + 11 REITs,
  country 'Hong Kong', 0 ticker collisions, 0 duplicates). The 29
  manual-review rows were not imported. Backups:
  `~/workspace/projects/jarvis-backups/assistant.db.2026-11-08.pre-14I-15J.bak`
  (pre-import, hash 812ECB53…89D4CD).
- 15J: all 7,625 US listings were already in `market_universe`
  (all generic 'equity'), so 15J was an UPDATE, not an insert.
  New module `agents/us_universe_import.py` (dry-run by default).
  Applied: **5,189 rows updated in place** (4,874 common_stock +
  315 adr), 0 unmatched, 0 new rows, 0 deletions. Backup taken
  before the update (post-14I, pre-15J).
- Universe after both imports: **10,619 rows** — USA 7,625
  (common_stock 4,874, adr 315, equity/unclassified 2,436),
  Hong Kong 2,793, Singapore 201. The 2,436 unclassified US rows
  are 1,050 special forms + 1,386 unknown form — they keep the
  generic 'equity' label so they stay identifiable for 15I/manual
  review.
- Known gap: BRK-A and BRK-B are unknown/unknown in v4_2 (no exact
  exchange evidence matched), so Berkshire stays unclassified as
  generic 'equity' for now — flagged, not silently fixed.
- 15H is DONE (validation via v3 audit + Phase 3 + import
  verification). 15I remains PENDING (specials held). 14H remains
  PENDING (29 HK manual-review rows). Phase A is otherwise closed;
  next is Step 20.

### 2026-11-08 — Step 20 cash flow and owner earnings (PARTIAL, not complete)

- Built `agents/financial_cashflow.py`: one-period and all-periods
  cash-flow assessment reusing Step 16 standardised values and
  Step 16's derived free cash flow (OCF − capex), Step 19's CAGR /
  YoY helpers and Step 17's TTM construction. Covers OCF quality
  (OCF / net income, OCF / operating income), capex (capex /
  revenue, capex / OCF, capex YoY, 5y/10y capex vs OCF CAGRs),
  FCF and FCF after dividends and after dividends + buybacks,
  SBC (SBC / OCF, SBC / FCF, SBC-adjusted FCF, labelled as an
  adjustment), working capital (change in working capital,
  working-capital requirement, cash conversion cycle from
  period-end balances), and 5y/10y cash-conversion trends. No
  price used anywhere; sign convention is Step 16's (capex /
  dividends / buybacks stored as positive outflows).
- Tests: `test_financial_cashflow.py` — **21 tests, OK** (109
  total with Steps 16–19, no regressions). Covers fixture
  arithmetic, owner-earnings unavailability, missing-input and
  zero-denominator behaviour, CCC components, and hand-checked
  MSFT FY2026 values on a temp DB copy. One first-run failure
  was a test bug (warnings-list index), fixed in the test.
  Production DB SHA-256 verified unchanged before and after
  (`e90fa34a…3c89dfe`, the post-import hash).
- MSFT FY2026 (verified from the DB): OCF $182,935m, capex
  $115,948m, FCF $66,987m, dividends $26,445m, FCF after
  dividends $40,542m, buybacks $22,271m, FCF after dividends +
  buybacks $18,271m, SBC $12,405m, SBC-adjusted FCF $54,582m.
  OCF / net income 1.37, FCF / net income 0.50, capex / revenue
  34.9%, capex / OCF 63.4%, SBC / OCF 6.8%, SBC / FCF 18.5%.
  Working capital fell $11,028m while revenue grew $50,115m
  (requirement −0.22 — revenue growth released working
  capital); cash conversion cycle −51.8 days (DIO 4.8, DSO 89.0,
  DPO 145.5).
- Answers the Step 19 question: FCF lags OCF because of a capex
  surge, not OCF weakness — 5y capex CAGR 41.2% vs OCF 19.0%
  vs FCF 3.6% (10y: 30.1% / 18.6% / 10.4%); FY2026 capex
  +79.6% YoY vs OCF +34.4%; Q1 FY2027 capex +103% YoY, FCF
  −27.4% YoY. TTM FCF to 2026-09-30 is $59,957m. Whether the
  capex is growth investment or rising maintenance spending
  cannot be answered from reported data (no maintenance/growth
  split).
- Owner earnings is UNAVAILABLE, not computed: D&A is not a
  Step 16 concept and no such key exists anywhere in the US raw
  data (verified across all US statement rows), capex is not
  split into maintenance vs growth, and no other non-cash-charge
  concept exists. No partial version is offered. FCF is the
  closest available proxy and is labelled as such — it is NOT
  owner earnings. Unlocking it needs D&A added to Step 16
  (SEC tag `DepreciationDepletionAndAmortization`) plus a
  maintenance-capex estimate.
- Remaining incomplete: MSFT only; OCF/FCF missing FY2014–
  FY2015 (sparse early cash-flow rows); TTM growth vs prior TTM
  not computed; no SG/HK analysis. **Step 20 is PARTIAL, not
  complete.** It completes Phase C's financial analysis engine
  (Steps 17–20) for the data available; its outputs feed Steps
  21 (valuation), 23 (margin of safety) and 24 (business quality).

### 2026-11-08 — Step 21 intrinsic valuation engine (PARTIAL, not complete)

- Built `agents/intrinsic_valuation.py`: a 10-year DCF engine
  wrapping the canonical `tools/valuation.py::calculate_dcf` (no
  second DCF built), with bear/base/optimistic scenarios, a WACC
  sensitivity table and an implied-growth inversion. Assumptions
  stated on every output: WACC 11%, terminal growth 2.5%,
  10-year forecast; scenario growth bear +3% (near 5y FCF CAGR
  3.6%), base +10% (near 10y FCF CAGR 10.4%), optimistic +15%
  (between 10y revenue CAGR 13.8% and 10y OCF CAGR 18.6%).
  A saved `valuation_profiles` row overrides the base assumptions
  when one exists (table currently empty; override exercised by a
  fixture test).
- Starting FCF is TTM free cash flow ($59,957m for MSFT), with the
  annual FY2026 variant ($66,987m) computed alongside and labelled.
  Net debt $15,200m and shares 7,446m from the latest balance
  sheet (2026-09-30). Price $507.12 as of 2026-11-03 (snapshot —
  stale, labelled).
- Tests: `test_intrinsic_valuation.py` — **26 tests, OK**
  (155 total with Steps 16–20, `python -m unittest discover`).
  Covers DCF arithmetic against an independent hand computation
  (base $163.30), scenario ordering, WACC monotonicity,
  implied-growth round-trip, unavailable-method behaviour and
  hand-checked MSFT values on a temp DB copy. Production DB
  SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash).
- MSFT (TTM basis): bear $98.52, base **$163.30**, optimistic
  $234.64; annual-FCF variant base $182.68. WACC sensitivity at
  base growth: 10% → $188.53, 11% → $163.30, 12% → $143.54.
  At $507.12 the price implies **~25.8% FCF growth a year for 10
  years** (TTM basis; ~24.2% annual basis; ~26.4% at the live
  $530.61 seen on the owner's PC on 2026-11-08, Yahoo price as of
  2026-11-06) — materially faster than the same method gave for
  NVDA (~17%) and AVGO (~18%). Margin of safety vs base is
  −67.8% at $507.12 — a number only; Step 23 interprets it, and
  no buy/sell call is made here. Full numbers in
  `step21-report.md`.
- Unavailable, not faked: owner earnings (no D&A concept, no
  maintenance/growth capex split), EV/EBITDA (no EBITDA/D&A),
  historical P/E (no historical price series stored — only a
  single 2026-11-03 snapshot row; the method is implemented for a
  caller-supplied price history and tested with fixture data).
  Unlocking owner earnings / EV/EBITDA needs D&A added to the SEC
  collector plus re-collection — a mapping change alone would give
  0% coverage on the data already stored, so it was not done here.
- **Step 21 is PARTIAL, not complete.** MSFT only; three methods
  unavailable as above; the valuation rests on one stale price
  snapshot. Sufficient to start Step 22 (sector-specific
  valuation) and Step 23 (margin of safety).

### 2026-11-08 — Step 22 sector-specific valuation models (PARTIAL, not complete)

- Built `agents/sector_valuation.py`: a sector router plus four
  sector model families.  Router resolution order: caller-supplied
  override > `market_universe` sector/industry > `reit`
  security_type > unknown.  Stored data findings that shaped it:
  (1) sector is NULL for every US/HK row (Singapore only), so
  MSFT resolves to "unknown" from stored data; (2) the stored
  "Financials" sector mixes banks, insurers, exchanges and other
  financials, so it is treated as ambiguous — no model is selected
  until the sub-sector is established.
- Banks: dividend discount (Gordon growth on DPS), excess-return /
  residual-income on book value (ROE vs cost of equity 10%), and
  justified P/B ((ROE − g)/(k − g) x BVPS), plus a historical-P/B
  check for caller-supplied price history.  Insurers: justified
  P/B (primary); embedded value and the underwriting/investment
  split unavailable (no premium / underwriting / investment /
  embedded-value concept in Step 16).  REITs: distribution DDM
  (dividends paid / shares); FFO/AFFO and NAV unavailable (no
  D&A, no property concepts).  Technology / general: the Step 21
  FCF DCF, delegated to `intrinsic_valuation.valuation_summary`
  (no second DCF built).
- The FCF DCF is refused for banks, insurers and REITs with an
  explicit reason (a bank's FCF is meaningless — deposits are raw
  material, debt is inventory; an insurer's FCF timing says
  nothing about value; a REIT's capex is mostly property
  acquisition).  Combined range in Step 21 format: tech/general
  = DCF scenarios; banks = min / excess-return / max of the
  three methods; insurers = justified P/B; REITs = DDM.
- Tests: `test_sector_valuation.py` — **37 tests, OK**
  (192 total with Steps 16–21, `python -m unittest discover`).
  Covers router normalisation, FCF-DCF refusals, DDM ($43.33),
  excess-return ($65.61) and justified-P/B ($66.67) arithmetic
  against hand-computed fixture values, unavailable-model
  behaviour, and MSFT/DBS/LINK REIT on a temp DB copy.
  Production DB SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash).
- Real-data results: MSFT technology path reproduces Step 21
  exactly (bear $98.52, base $163.30, optimistic $234.64;
  implied growth ~25.8%).  MSFT as an as-if bank cross-check:
  DDM $74.51, justified P/B $219.49, excess-return $706.94 —
  the last is the honest warning about that model (it assumes
  a 30% ROE persists 10 years).  DBS/OCBC/UOB with
  `sector_override='bank'`: models selected, all missing (SG
  rows are metadata only, 0 concepts); without the override
  DBS resolves to "financial" (ambiguous) and nothing is
  selected.  Great Eastern (insurance): everything missing.
  LINK REIT (00823): routes to "reit", everything missing.
  Full numbers in `step22-report.md`.
- **Step 22 is PARTIAL, not complete.** Only the
  technology/general path is validated on real data (MSFT);
  bank, insurer and REIT models are fixture-only until a
  bank/insurer/REIT has validated statement line items (SG
  extraction 11D still PAUSED; no HKEX statements).  Cost of
  equity (10%) and the DDM growth cap (5%) are uncalibrated
  judgement defaults.  Sufficient to start Step 23 (margin of
  safety and relative valuation).

### 2026-11-08 — Step 23 margin of safety and relative valuation (PARTIAL, not complete)

- Built `agents/margin_of_safety.py`: margin of safety vs each
  point of the Step 21/22 range, entry-price ladder, tier logic,
  material-deterioration flag wired to Steps 18/19/20,
  WACC/growth sensitivity, and a relative P/E cross-check.
  No buy/sell call anywhere; the final decision is the owner's.
- **Convention warning, recorded here because it matters:**
  Step 23 defines margin the Graham way, `(value - price) /
  value`.  Steps 21/22 used `(value - price) / price` (MSFT vs
  base was -67.8% there; Step 23 gives -210.6%).  Both numbers
  are shown in every output, labelled, so the two conventions
  are never confused.
- Tier logic (owner's standing rule): `strongest_alert` =
  margin vs conservative value >= 20% (price at or below 80%
  of conservative value) AND no material deterioration —
  exactly 20% qualifies.  `reasonable_price` = price at or
  below base value but not an alert.  `fairly_valued` = price
  above base but at or below optimistic value.  `overvalued` =
  price above optimistic value.  `insufficient_data` =
  conservative value or price missing.  Each verdict carries
  reasons and the conditions that would change it.
- Deterioration rule (explicit, not guessed): TRUE when Step 18
  health verdict is `weak` (or fell to `weak`), latest annual
  net income is negative, latest annual revenue fell >10% AND
  net income fell >20%, or latest annual OCF is negative.  NOT
  deterioration on its own: a `strong`/`adequate` verdict, FCF
  falling alone (MSFT FY2026 FCF -6.5% on a capex surge), a
  working-capital decline alone (a Step 18 watch item), or a
  one-off earnings dip.  Missing inputs make the flag UNKNOWN
  and block a strongest alert.  A caller-supplied override is
  supported and recorded as the source.
- Entry-price ladder: prices at 0% / 20% / 30% / 40% below the
  conservative value, with the implied FCF growth at each rung
  (Step 21 inversion) for the DCF path, or implied dividend
  growth (Gordon inversion) for bank/REIT paths.  Sensitivity:
  margin across WACC 10%/11%/12% x growth +3%/+10%/+15% (DCF),
  cost of equity 9%/10%/11% on the DDM (bank/REIT).
  Relative check: current P/E vs the P/E each range point would
  imply at latest diluted EPS.
- Tests: `test_margin_of_safety.py` — **36 tests, OK**
  (228 total with Steps 16–22, `python -m unittest discover`).
  Covers margin arithmetic and both conventions, ladder prices
  and implied growth, tier boundaries (exactly 20% below =
  alert, 19.9% = not), deterioration blocking (deteriorated and
  unknown), sensitivity monotonicity and Step 21 reproduction,
  and hand-checked MSFT values on a temp DB copy.  Two failures
  found and fixed during the run: a real module bug (an
  `extra={"value": ...}` overwrote the result's value in
  `margins_for_range`) and a test bug (Step 21's $98.52 was
  rounded; exact 98.5207), plus a second real bug found in the
  same pass (the YoY helper read dict key `"growth"` where Step
  19 uses `"value"`, so the FCF caution never fired) and a
  third (latest net income was only loaded in the OCF fallback
  path, leaving it None when OCF was found).  Production DB
  SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash).
- MSFT (price $507.12 as of 2026-11-03, stale, labelled):
  margins vs conservative -414.7%, vs base -210.6%, vs
  optimistic -116.1% (Step 23 convention).  **Tier:
  `overvalued`** — price above the optimistic value, with no
  material deterioration (health STRONG, revenue +17.8%, net
  income +31.3% in FY2026), so the tier is driven by price
  alone.  Entry ladder: $98.52 (implied FCF growth +3.0%),
  $78.82 at 20% (implied -0.1% — the strongest-alert threshold
  price), $68.96 (-2.0%), $59.11 (-4.3%).  Sensitivity: even
  the most generous cell (10% WACC, +15% growth, $273.27) is
  46% below the price — the verdict holds across the whole
  grid.  Relative P/E: current 28.25 vs base-implied 9.1x.
  DBS returns `insufficient_data` (no statements).  Full
  numbers in `step23-report.md`.
- **Step 23 is PARTIAL, not complete.** MSFT only; no
  `strongest_alert` has ever fired on real data (the alert and
  deterioration-blocking paths are fixture-tested only);
  bank/REIT ladder and sensitivity are fixture-only (DBS has
  no statements); the valuation rests on one stale price
  snapshot; tier thresholds are the owner's standing rules,
  uncalibrated.  Sufficient to start Step 24 (competitive
  advantage and business quality).

### 2026-11-08 — Step 24 competitive advantage and business quality (PARTIAL, not complete)

- Built `agents/business_quality.py`: seven scored components
  (returns on capital, margin stability, efficiency, growth
  consistency, earnings quality, balance-sheet strength,
  capital allocation), each 0/1/2 with its inputs shown, and
  an overall rating — `excellent` / `good` / `adequate` /
  `weak` / `insufficient data` — from the mean component score
  (>= 1.6 / >= 1.0 / >= 0.5).  Reuses Step 17 ratios, the Step
  18 health verdict, Step 19 growth and Step 20 cash flow;
  never re-derives a value.  No price used, no network
  requests.  No buy/sell call anywhere.
- Moat statement is explicitly qualified: "strong financial
  evidence of a durable advantage" is evidence, not proof.
  Seven things statements cannot assess are listed on every
  assessment (brand, network effects, switching costs,
  regulatory moats, management integrity, competitive position
  vs peers, customer concentration), each with its reason.
- Two honest guards: incremental ROIC is computed only when
  invested capital grew (on a shrinking base it is missing,
  never a nonsense number — caught by the weak fixture), and
  every window ends at the assessed period, never at the
  latest (a real bug found during testing: the first version
  used latest-period windows for every historical year).
  A third real bug found in the same pass: the dividend-cut
  count zipped a list with itself when shorter than 11 years,
  so cuts were never counted — fixed and regression-tested.
- Quality gate wired into Step 23: `classify_tier` takes an
  optional `quality` argument; a `weak` or `insufficient
  data` rating blocks a `strongest_alert` even at a discount,
  like material deterioration.  No quality supplied keeps the
  previous behaviour (all 36 Step 23 tests still pass);
  `margin_of_safety_summary` always supplies one and now
  returns `business_quality` in its output.
- Tests: `test_business_quality.py` — **15 tests, OK**
  (243 total with Steps 16–23, `python -m unittest discover`).
  Covers a synthetic weak business (rating `weak`, all six
  scored components 0), a synthetic solid mid business
  (rating `good`, mean 10/7), insufficient data (< 3 periods,
  no rows, DBS), the quality gate (weak / insufficient-data
  block, good allows, none keeps old behaviour, blocked alert
  falls back to `reasonable_price`), and hand-checked MSFT
  values on a temp DB copy.  Production DB SHA-256 verified
  unchanged before and after (`36862922…089187`, post-audit
  hash).
- MSFT FY2026: **rating `excellent`, mean score 2.0 (7/7
  components scored 2)** — 5y mean ROIC 33.9% (every year >=
  30.6%), 5y net margin mean 36.6% (CV 0.062), gross margin
  mean 68.8% (CV 0.010), CCC -51.8 days, revenue steady with
  10/10 years growing and 3.3% max drawdown, 5y mean OCF/net
  income 1.30 and FCF/net income 0.75, 10/10 profitable years,
  health `strong`, dividends up 18 consecutive years with 0
  cuts, shares -2.0% over 5y, incremental ROIC 5y 23.5% /
  10y 30.2%, payout 36%.  History: `good` FY2010–FY2022,
  `excellent` from FY2023; dips to 1.0 in FY2015 and FY2018
  are the two known one-offs.  One caution inside the
  excellent rating: latest-year FCF/net income is 0.50 on the
  capex surge (Step 20 finding).  DBS returns `insufficient
  data`.  Full numbers in `step24-report.md`.
- **Step 24 is PARTIAL, not complete.** MSFT only (plus two
  synthetic fixtures); the moat conclusion is financial
  evidence only; scoring thresholds are judgement rules,
  uncalibrated against a peer set (none exists).  Sufficient
  to start Step 25 (global market scanner).

### 2026-11-08 — Step 25 global market scanner (PARTIAL, not complete)

- Built `agents/market_scanner.py`: orchestrates the Step 16-24
  pipeline over a ticker selection and returns a ranked
  candidate list.  It re-derives nothing — per ticker it calls
  the Step 23 margin-of-safety summary (Step 21/22 valuation,
  Step 18/19/20 deterioration flag, Step 24 quality gate) plus
  the Step 18 health verdict.  Prices are caller-supplied or
  fall back to the latest `financial_snapshots` row, labelled
  with its as-of date; no live price feed, no network request,
  no database writes.  Starting FCF basis is TTM when the data
  supports it, otherwise the labelled annual basis.
- Ranking: `strongest_alert` first, then `reasonable_price`,
  `fairly_valued`, `overvalued`, `insufficient_data` last.
  Within a tier, tickers not surfaced recently go first
  (fresh-name rotation), then the largest margin of safety vs
  the conservative value, then the best quality rating.
  Rotation never promotes across tiers.  No buy/sell call
  anywhere.
- Performance: tickers with no statement rows skip the
  pipeline entirely (one `GROUP BY` up front); only tickers
  with statements run it, with a per-run cache.  Full
  10,619-row universe scans in **~1.1 seconds**.
- Tests: `test_market_scanner.py` — **25 tests, OK**
  (268 total with Steps 16-24, `python -m unittest discover`).
  Covers price normalisation, ranking order, rotation
  (including never promoting across tiers), a synthetic
  cheap-and-good business reaching `strongest_alert`
  (conservative $13.39, margin 62.6%, quality good), a
  synthetic cheap-but-weak business NOT reaching it
  (conservative $0.50, margin 39.7%, quality weak, tier
  `reasonable_price`, blocked), a weak business with no value
  (`insufficient_data`), no-statements / no-price handling,
  snapshot-price loading, and MSFT/D05/full-universe on a
  temp DB copy (production SHA-256 verified unchanged before
  and after, `36862922…089187`, and asserted in the tests'
  `tearDownClass`).
- Full-universe scan (temp copy, snapshot prices): selected
  10,619 — **scannable 1** (MSFT only).  USA 7,625 (overvalued
  1, insufficient_data 7,624), Singapore 201 (all
  insufficient_data), Hong Kong 2,793 (all insufficient_data).
  10,617 tickers have no financial statement rows; D05 has
  rows but no price and no conservative value.  Rank 1: MSFT
  (overvalued, quality excellent, health strong, TTM basis,
  range $98.52 / $163.30 / $234.64, price $507.12 as of
  2026-11-03).  No strongest alert, no reasonable-price name
  on real data.
- Bug found and fixed during testing: the per-run cache was
  keyed by ticker only, so six dual-listed tickers (AWX, BHK,
  BMA, BSL, HMN, OGS — USA and Singapore) collapsed into one
  candidate and the coverage showed USA 7,619 / Singapore
  207.  The cache key now includes the exchange, with a
  regression test; coverage now matches the database exactly
  (USA 7,625 / Singapore 201).
- **Step 25 is PARTIAL, not complete.** Only 1 of 10,619
  tickers is scannable — the scanner reports that honestly,
  but it cannot surface fresh names until statements and
  prices exist for more tickers (US statements beyond MSFT
  need full-universe SEC enrichment, 15F, NOT authorised
  beyond the pilot; SG needs line-item extraction, 11D
  PAUSED; HKEX needs a statement collector; prices need a
  feed or caller-supplied prices, Step 38 concern).  Sector
  is NULL for every US/HK row, so the Step 22 router still
  depends on caller-supplied overrides.  Ranking thresholds
  and rotation are uncalibrated judgement rules.  Not wired
  into `main.py` (Step 50).  Sufficient to start Step 26
  (undervalued-company detection).

### 2026-11-08 — Step 26 undervalued-company detection (PARTIAL, not complete)

- Built `agents/undervalued_detector.py`: a focused detector on
  the Step 25 scanner — it re-derives nothing.  It filters scan
  results into candidates (strongest_alert tier, plus two watch
  bands: `watch_discount` 0–20% below conservative value and
  `watch_below_base` below base but above conservative value,
  both with the same quality-adequate-or-better and
  deterioration-known-clear gates as an alert), value traps
  (price at/below conservative value on a weak or deteriorating
  business, reported with the evidence, never a candidate), and
  a "why not" explanation (`explain_ticker`) for every other
  ticker.  Detection records carry the valuation range, both
  margin conventions labelled (Step 23 Graham vs Step 21/22),
  quality, health, deterioration flag and signals, price with
  as-of date and source, reasons it qualifies and reasons it
  does NOT qualify for anything stronger.  No buy/sell call
  anywhere.  `briefing_line` / `briefing_summary` give
  alert-ready one-line-per-candidate output for a daily check
  or morning briefing — records only, never sent or scheduled.
  Run history is a JSON-lines file log
  (`data/detection_runs.jsonl`, `append_run_log` /
  `read_run_log`), not a production-table write.
- Tests: `test_undervalued_detector.py` — **22 tests, OK**
  (290 total with Steps 16–25, `python -m unittest discover`).
  Covers strongest-alert detection (incl. the just-above-20%
  boundary), both watch bands, watch requiring adequate
  quality, value-trap rejection (cheap-but-weak) and a weak
  business with no value not being a trap, "why not"
  explanations (overvalued / fairly_valued / insufficient_data
  / blocked_alert / reasonable-price weak quality), mixed-market
  candidates + traps + insufficient together, briefing output,
  rotation passing through from the scanner, run-log write/read,
  and MSFT/D05/full-universe on a temp DB copy (production
  SHA-256 verified unchanged before and after,
  `36862922…089187`, and asserted in the tests'
  `tearDownClass`).
- Bugs found and fixed during testing: (1) the detector's
  re-sort by margin discarded the scanner's fresh-name
  rotation — candidates are now ranked by band, then by the
  scanner's rank within the band; (2) the exact-20% boundary
  is a floating-point edge — a price at exactly 80% of the
  conservative value computes a margin of
  0.19999999999999993, just below Step 23's `>= 20%` test, so
  it lands in watch_discount; Step 23's threshold logic was
  left unchanged and the edge is documented, not hidden.
- Detector result on real data (temp copy, 2026-11-03 snapshot
  prices, ~1.2 seconds): selected 10,619 — scannable 1 (MSFT
  only).  **Candidates: 0** (0 strongest alerts, 0 watch).
  **Value traps: 0.**  By category: overvalued 1 (MSFT),
  insufficient_data 10,618.  MSFT is not a candidate and not a
  trap (tier overvalued, quality excellent, health strong);
  D05 is insufficient_data.
- **Step 26 is PARTIAL, not complete.** No real candidate has
  ever been detected — the detector is fixture-proven, but only
  1 of 10,619 tickers is scannable (same data gap as Step 25:
  15F NOT authorised beyond the pilot, 11D PAUSED, no HKEX
  statement collector, no price feed).  No value trap has ever
  been found on real data either, for the same reason.  The
  exact-20% floating-point boundary is a known edge.  The
  project run log has no entries yet.  Not wired into `main.py`
  (Step 50); no sending or scheduling.  Sufficient to start
  Step 27 (financial risk and deterioration detection).

### 2026-11-08 — Step 27 financial risk and deterioration detection (PARTIAL, not complete)

- Built `agents/deterioration.py`: a four-area deterioration
  assessment (trend breaks, margin erosion, leverage/liquidity
  drift, cash-flow stress) reusing the Step 18 health, Step 19
  growth and Step 20 cash-flow assessments — nothing
  re-derived.  Each area gets a verdict (`deteriorating` /
  `warning` / `stable` / `improving` / `insufficient data`)
  with its evidence, plus named risk flags with severity
  (`high` for deteriorating areas, `medium` for warning).
  Overall rule (explicit, stated on every output): material
  deterioration is TRUE only when the overall verdict is
  `deteriorating` — two or more areas deteriorating, OR one
  deteriorating area plus a Step 18 health verdict of `weak`,
  OR latest annual net income < 0, OR latest annual OCF < 0.
  One deteriorating area alone, or two or more warning areas,
  is a `warning` overall (material FALSE).  A working-capital
  fall with the current ratio still above 1, an FCF fall
  alone, and a recovered one-off dip are warnings/cautions,
  never deterioration on their own.
- One-offs vs trends: a dip year is an "identified one-off"
  when the level recovers past the pre-dip level within two
  years.  MSFT's history names 13 of them, including FY2015
  net income −44.8% (Nokia write-down, recovered by FY2017)
  and FY2018 −35.0% (tax charge, recovered by FY2019).  A dip
  in the latest year has no following year yet, so it is a
  warning — recovery cannot be observed yet.
- Step 23 wiring: `margin_of_safety.assess_deterioration`
  takes an optional `deterioration_27` argument; when supplied
  and usable the Step 23 flag takes its boolean from Step 27
  and its signals from Step 27's high-severity flags,
  corroborated against Step 23's simpler rule from the same
  inputs (a disagreement is recorded as a caution, never
  hidden).  When Step 27 cannot assess a ticker, Step 23
  keeps its existing Step 18/19/20 logic unchanged.
  `margin_of_safety_summary` computes Step 27 (guarded, like
  business quality) and returns it as `deterioration_27`.
- Tests: `test_deterioration.py` — **24 tests, OK**
  (314 total with Steps 16–26, `python -m unittest discover`).
  Covers trend breaks (sustained decline vs recovered
  one-off), margin erosion, leverage drift (working-capital
  fall as warning, current ratio < 1 as deteriorating),
  cash-flow stress (payouts exceeding FCF, capex surge),
  the overall rule (one deteriorating area alone = warning,
  negative net income = hard distress), insufficient data
  (no rows, two periods, DBS), MSFT hand-checked values on
  a temp DB copy, the Step 23 wiring (MSFT tier unchanged,
  Step 27 deteriorating blocks via Step 23, Step 27
  insufficient falls back to the old rule, old path
  unchanged without Step 27).  Two failures found and fixed
  during the run: both were test/module calibration, not
  logic errors — the cash-flow area raised a generic "FCF
  conversion falling" flag when the causal "capex surge
  compressing FCF" flag also fired (the capex flag now takes
  precedence), and the solid fixture's 5y net-margin gain
  (+1.6pp) fell short of the +2pp "improving" threshold
  (fixture net growth raised to +14%/yr).  Production DB
  SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash).
- MSFT FY2026: overall verdict **warning**,
  `material_deterioration` **False** — three areas warning,
  none deteriorating.  Trend breaks warning (revenue +17.8%,
  net income +31.3%, OCF +34.4%; FCF −6.5% YoY, 3 declines
  in 5y, 5y CAGR still +3.6% — a capex effect); margins
  improving (net margin 40.3%, +3.6pp over 5y, CV 0.062);
  leverage warning (working capital −59.4% over 5y, current
  ratio 2.08 → 1.23, still above 1; debt −30.7% over 5y,
  debt/OCF 0.22x); cash-flow warning (FCF/net income 0.50 vs
  5y mean 0.75; capex/OCF 63.4% vs 26.9% 5y ago; FCF after
  dividends and buybacks still +$18,271m).  Risk flags, all
  medium: "free cash flow softening", "working capital
  falling", "capex surge compressing FCF".  Step 23 result
  unchanged (tier overvalued, no deterioration).  DBS
  insufficient data.  Full numbers in `step27-report.md`.
- History check: `assess_all_periods` over 19 annual periods
  — FY2008–09 insufficient data, FY2010–11 stable, FY2012–14
  warning, **FY2015–16 deteriorating** (the Nokia write-down
  years, correctly flagged point-in-time before the recovery
  was observable), FY2017–26 warning.  Calibration caveat:
  the overall verdict has been `warning` for MSFT in every
  year since 2012 except 2015–16, because working capital
  has fallen and capex/OCF has risen for a decade — the
  warning state is persistent, not a new event, and whether
  it should require a *change* in trend rather than its
  *level* is an open calibration question for a future step.
- **Step 27 is PARTIAL, not complete.** MSFT only on real
  data (plus synthetic fixtures); the `deteriorating`
  overall verdict is fixture-proven, never observed on a
  real company.  Thresholds are judgement rules,
  uncalibrated against a peer set or a genuinely distressed
  issuer (none exists in the DB).  The module cannot see
  anything statements cannot show (off-balance-sheet risk,
  refinancing walls, customer concentration, management
  intent).  Not wired into `main.py` (Step 50); the scanner
  and detector consume the Step 23 flag (which now carries
  the Step 27 verdict) but do not yet surface the named risk
  flags directly.  Sufficient to start Step 28 (portfolio
  construction and position sizing).

### 2026-11-08 — Step 28 portfolio construction and position sizing (PARTIAL, not complete)

- New module: `agents/portfolio_construction.py`; tests:
  `test_portfolio_construction.py` — **44 tests, OK** (358 total
  with Steps 16–27, `python -m unittest discover`, run from the
  project root, no regressions).  Full report:
  `~/workspace/projects/step28-report.md`.
- **Roadmap note:** `AGENTS.md` section 10 lists Step 28 as
  "company comparison and competitor analysis".  This build
  follows the Step 28 brief as given for this step — portfolio
  construction and position sizing — and the AGENTS.md line has
  been updated to match what was actually built.  If company
  comparison is still wanted, it belongs to a later step number.
- Sizing methods, all with maths shown and assumptions stated:
  equal weight (default, water-filling against a 20%
  per-position cap, whole shares, leftovers stay cash), Kelly
  criterion (f* = p - (1-p)/b, half-Kelly by default, capped at
  25% of capital, edge caller-supplied per ticker — Jarvis does
  not estimate win probabilities; a candidate without a supplied
  edge is skipped), fixed fraction.  `build_from_detection`
  takes a Step 26 detection result directly; value traps are
  excluded and counted.
- **$1,000 small-cap basket:** sized by the amount the owner is
  willing to lose — a position's cost IS its maximum loss, every
  position records `amount_at_risk`, and the accept-the-loss
  discipline is stated on the output.  Currency is an input,
  never assumed (SGD vs USD not confirmed); a basket without a
  currency carries a warning.
- Diversification checks (single name > 25%, sector > 40%,
  country > 60% — judgement thresholds, stated on the output)
  and a rebalancing view (caller-supplied holdings, target
  weights, prices; whole shares, sells fund buys; proposal
  only).  Proposals can be saved/loaded as JSON at a
  caller-chosen path.  No existing portfolio storage module
  exists (no `agents/portfolio.py`, no portfolio table), so this
  module is new.  No price fetching, no network, no production
  DB writes, no trade ever placed — every output is a proposal
  with the disclaimer that a trade needs the owner's approval
  for that specific exact order.
- **Fixture example (verified by probe + tests):** two
  strongest-alert fixtures (FIXA/FIXB, MID_SPEC at $5.00),
  $10,000 capital, equal weight, 20% cap → 400 shares each,
  $2,000 each, invested $4,000, cash $6,000 (only 40% invested
  because the cap binds with two candidates — the proposal says
  so, with concentration warnings).  $1,000 basket at $200 max
  loss each → 40 shares each, $200 at risk each, $400 invested,
  $600 cash.  Kelly example: 60% win probability, 2:1 payoff →
  half-Kelly 20% of capital.
- **Real-data run (temp copy of the production DB):** detection
  with snapshot prices finds 0 candidates and 0 traps, so the
  builder proposes nothing — 0 positions, $0.00 invested,
  $10,000.00 cash left over, reason stated.  MSFT is overvalued
  with excellent quality, so no allocation is proposed for it.
- **Bug found and fixed during testing:** Kelly fractions were
  left as raw floats, so `0.4 * 0.5` came out as
  `0.19999999999999998` and a $2,000 target bought 199 shares
  instead of 200.  Fractions (and the edge) are now rounded to 6
  decimals; the two tests that caught it pass after the fix.
- **Step 28 is PARTIAL, not complete.** It has never sized a
  real candidate (same data gap as Steps 25–26: 15F NOT
  authorised beyond the pilot, 11D PAUSED, no HKEX statement
  collector, no price feed).  Kelly edges are owner judgement by
  design and cannot be validated here.  Concentration thresholds
  and the 20% cap are uncalibrated judgement defaults, and
  sector is NULL for every US/HK row, so sector concentration
  cannot be assessed on real data.  Not wired into `main.py`
  (Step 50); no sending or scheduling; no live prices (Step 38
  concern).  Sufficient to start Step 29 (automated company
  research reports).

### 2026-11-08 — Step 28b company comparison and competitor analysis (PARTIAL, not complete)

- New module: `agents/company_comparison.py`; tests:
  `test_company_comparison.py` — **13 tests, OK** (371 total
  with Steps 16–28, `python -m unittest discover`, run from the
  project root, no regressions).  Full report:
  `~/workspace/projects/step28b-report.md`.
- **Why 28b:** `AGENTS.md` section 10 lists Step 28 as
  "company comparison and competitor analysis", but Step 28 was
  built as portfolio construction and position sizing per the
  Step 28 brief.  Company comparison is built here as Step 28b
  so the later numbering (Step 29 research reports, Step 30
  thesis tracking, etc.) is unchanged.
- The comparator orchestrates Steps 17–24 and 27 and re-derives
  nothing: per-company profiles with size (revenue, market cap),
  profitability (margins, ROE, ROA, ROIC), growth (5y/10y
  CAGRs, latest YoY), health (Step 18 verdict, D/E, current
  ratio, debt/OCF, debt/FCF), valuation (P/E, P/B, P/S, EV/EBIT,
  DCF conservative/base range), quality rating and
  deterioration verdict — every metric carrying its period and
  missing-data reason.  Per-metric dense ranking (rank 1 best;
  lower better for D/E, debt/OCF, debt/FCF, P/E, P/B, P/S,
  EV/EBIT); a peer is ranked only where the metric exists for
  it, with the ranked count stated.  "Stands out" / "lags" only
  where at least three tickers are ranked.  No buy/sell call, no
  "best stock" verdict.
- **Peer-set rules:** target with no statements → whole
  comparison `insufficient_data`; peers with no statements are
  listed as not comparable with exact missing inputs, never
  silently dropped; fewer than two peers with data → values
  shown but no ranking presented as meaningful.  Optional
  sector filter against stored `market_universe` sectors.
  Prices caller-supplied, or the latest `financial_snapshots`
  row as a labelled fallback (shared by the price ratios and
  the DCF range).  No network, no writes.
- **Real-data run (temp copy of the production DB):** MSFT vs
  NVDA / AVGO / GOOGL — comparable in form, ranking not
  meaningful: no peer has statements.  MSFT profile verified
  against Steps 17–24/27 (revenue $331,839m, market cap
  $3.776tn at $507.12 as of 2026-11-03, net margin 40.31%,
  ROIC 30.59%, P/E 28.25, DCF base $163.30, quality excellent,
  health strong, deterioration warning, not material).  DBS
  (D05) has rows but no standardised concepts, so its metrics
  are all missing with reasons.
- **Fixture example (verified by probe + tests):** MID ($5.00)
  vs BIG ($20.00) vs WEAK ($0.30) — BIG largest and most
  profitable, MID cheapest on P/E (3.23) and tied best on
  current ratio (1.35), WEAK last on every metric (health weak,
  quality weak, deterioration deteriorating).  10y CAGRs missing
  for all three fixtures (6 annual periods cannot support an
  11-period CAGR), reason recorded.
- **Bugs found and fixed during testing:** Step 19 CAGR
  entries are dicts, not floats (extractor now takes
  `["value"]`); `market_cap_for` takes a price-input dict and
  returns a `(value, method)` tuple (was passed a bare float);
  price ratios were missing when no price was supplied because
  `compute_ratios` has no snapshot fallback while
  `valuation_summary` does — the profile now resolves the
  snapshot price once, up front; revenue/market cap are raw
  currency units, not millions (unit labels corrected).
- **Step 28b is PARTIAL, not complete.** No real peer
  comparison has ever run (same data gap as Steps 25–26: 15F
  NOT authorised beyond the pilot, 11D PAUSED, no HKEX
  statement collector, no price feed).  The sector filter
  cannot work on real data (sector NULL for every US/HK row).
  Ranking thresholds are uncalibrated judgement rules.  Not
  wired into `main.py` (Step 50).  Sufficient to start Step 29
  (automated company research reports), which can consume
  these comparisons.

### 2026-11-08 — Step 29 automated company research reports (PARTIAL, not complete)

- New module: `agents/research_report.py`; tests:
  `test_research_report.py` — **13 tests, OK** (384 total
  with Steps 16–28b, `python -m unittest discover`, run from
  the project root, no regressions).  Full report:
  `~/workspace/projects/step29-report.md`; generated MSFT
  report: `~/workspace/projects/step29-msft-report.md`.
- The generator composes Steps 16–28b and re-derives
  nothing: header (ticker, company, sector, country, report
  date, price with as-of date), business summary, financials
  (Step 16–20 key figures, ratios, growth, cash flow in
  tables with periods), health and quality (Step 18 verdict,
  Step 24 rating with all seven components, Step 27
  deterioration verdict and risk flags, plus the seven things
  statements cannot assess), valuation (Step 21/22 range,
  Step 23 margins in both conventions, tier, entry-price
  ladder, strongest-alert threshold price, relative P/E),
  peer comparison (Step 28b), risks and what would change
  the view, and a one-paragraph conclusion.  Every section
  carries its source and period; a section that cannot be
  produced is present with its status and reason, never
  silently omitted.  No recommendation anywhere; generated
  report text contains no "buy" or "sell" as standalone
  words (word-boundary regex checked on every report
  variant, dict and Markdown).
- **Business description: none exists.** No description
  column exists anywhere in the database, so the business
  summary says so and sticks to stored sector/industry/
  listing labels.  No description is invented or fetched.
- Report status is driven by the three core data sections
  (financials, health/quality, valuation): all three
  insufficient → `insufficient_data`.  Reports are written
  as Markdown files (default `reports/` under the project
  root); generation opens the database read-only (`mode=ro`),
  so it can never write to it.
- **MSFT report (peers NVDA/AVGO/GOOGL):** status partial
  (no description; no peer has statements).  Tier
  **overvalued**, quality **excellent**, health **strong**,
  deterioration **warning** (not material); price $507.12
  as of 2026-11-03; range $98.52 / $163.30 / $234.64;
  strongest-alert price $78.82.  Every number verified
  against Steps 17–24/27 in the tests (revenue $331,839m,
  net income $133,749m, P/E 28.25, P/B 8.54, EV/EBIT 24.38).
  Risks: FCF softening, working capital falling, capex
  surge compressing FCF.
- **DBS report:** `insufficient_data` — 79 statement rows
  are announcement metadata only (0 concepts), so
  financials (15/15 key figures missing), health/quality
  (all three assessments insufficient) and valuation (no
  conservative value) each say so with their specific
  reason.  Unknown ticker (ZZZZ) behaves the same way.
- **Bugs found and fixed during testing:** (1) real module
  bug — `business_quality as bq` was never imported, so the
  quality section silently came back empty (MSFT quality
  "unknown"); (2) real module bug — valuation defaulted to
  the TTM FCF basis, so a ticker with annual rows only got
  no DCF range; it now falls back to the labelled annual
  basis (same fallback as the Step 25 scanner) and records
  the basis used; (3) real module bug — the health/quality
  section counted as "complete" when the three assessments
  returned dicts even when all three said insufficient data;
  the status now checks the verdicts; (4) real module bug —
  report status treated every section equally, so the
  always-partial business summary made `insufficient_data`
  unreachable; status is now driven by the core sections;
  (5) test bugs (peer summary wording, no-price
  `missing_inputs` substring check, `$98m` vs `$98.52`
  money format).
- **Housekeeping:** `/tmp` tmpfs filled to 84% with stale
  temp DB copies from the test runs; cleaned to 0% during
  Step 29 (the suite leaves ~20MB of temp DB copies behind
  per full run — a housekeeping gap, same as Step 27).
- **Step 29 is PARTIAL, not complete.** MSFT only on real
  data (plus a synthetic SOLID fixture); no business
  description exists in the database; the report rests on
  one stale price snapshot; not wired into `main.py`
  (Step 50); no sending or scheduling.  Sufficient to start
  Step 30 (investment thesis tracking).

### 2026-11-08 — Step 30 investment thesis tracking (PARTIAL, not complete)

- New module: `agents/thesis_tracking.py`; tests:
  `test_thesis_tracking.py` — **26 tests, OK** (410 total
  with Steps 16–29, `python -m unittest discover`, run from
  the project root with
  `~/workspace/projects/jarvis-venv/bin/python`, no
  regressions).  Full report:
  `~/workspace/projects/step30-report.md`; example MSFT
  thesis file: `~/workspace/projects/step30-msft-thesis.json`.
  Production DB SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash).
- A thesis records the owner's reasoning in the owner's own
  words — blank thesis text raises `ValueError`; the module
  never invents it.  Creation snapshots the Step 21–27
  evidence (valuation range, tier, entry-price ladder, alert
  price, quality, deterioration, health verdict, latest key
  figures) via `margin_of_safety_summary`.  Confirm /
  invalidate conditions are caller-supplied where given,
  otherwise suggested defaults from the snapshot (Step 23
  alert price and tier, Step 24 quality, Step 27
  deterioration), labelled as such.  Conditions are free
  text plus an optional machine-checkable `rule` (11 rule
  kinds); a condition with no rule, an unknown rule kind, or
  a missing input is reported as "not machine-checkable —
  owner to judge", never guessed at.
- Review re-runs the pipeline, compares the new snapshot
  with the creation snapshot field by field, evaluates the
  conditions and suggests a status: `invalidated` when any
  invalidate condition is met, `confirmed` when every
  checkable confirm condition is met, `stale` when no fresh
  snapshot could be built, otherwise `active`.  Statuses:
  `active` / `confirmed` / `invalidated` / `stale` /
  `closed`, every change recorded in `status_history`.
  No recommendation anywhere; review output contains no
  "buy"/"sell" as standalone words (tested).
- Storage is JSON files, one per thesis, under a
  caller-chosen directory (default `data/theses/` under the
  project root — the same file-log pattern Step 26 uses for
  `data/detection_runs.jsonl`).  No new production database
  tables were created and nothing was written to the
  production database.  Existing history storage
  (`agents/valuation_history.py`) was checked and does not
  fit: it writes valuation snapshots into the production
  database, which the Step 30 brief forbids for thesis
  records.
- **Design decision worth recording:** a default condition
  already met in the creation snapshot is skipped (a thesis
  cannot be confirmed or invalidated by the state it was
  created in) and listed in
  `conditions_skipped_already_met`.  Caught by the MSFT
  example: the "tier becomes overvalued" invalidate
  candidate is already true for MSFT, so carrying it as a
  live condition would have invalidated the thesis on its
  first review.
- **Annual-FCF fallback:** like the Step 25 scanner and the
  Step 29 report, the snapshot falls back to the labelled
  annual FCF basis when a ticker has no quarterly rows (no
  TTM free cash flow exists).  Found by probing: the solid
  fixture has annual rows only and the TTM-first summary
  returned no valuation range at all.
- **Example MSFT thesis** (labelled as an example, not the
  owner's words; file `step30-msft-thesis.json`): status
  `active`, tier **overvalued**, quality **excellent**,
  health **strong**, deterioration **warning** (not
  material); range $98.52 / $163.30 / $234.64;
  strongest-alert price $78.82; price $507.12 as of
  2026-11-03; revenue $331,839m, net income $133,749m,
  FCF $66,987m.  3 confirm / 2 invalidate conditions, 1
  skipped.  Review on unchanged data: **active**, no
  changes.  Review at a hypothetical $70: **confirmed**,
  3/3 confirm conditions met.  **DBS thesis: `stale`**,
  snapshot `insufficient_data`.
- **Step 30 is PARTIAL, not complete.** No real thesis has
  been tracked over time — the only thesis ever created is
  the labelled MSFT example, reviewed once on unchanged
  data and once at a hypothetical price.  The rules cover
  price, tier, quality, deterioration, margin and
  conservative value only; narrative conditions (management,
  moat, competition) stay as free text for the owner to
  judge.  Not wired into `main.py` (Step 50); no sending or
  scheduling of reviews.  Sufficient to start Step 31
  (market news collection).

### 2026-11-08 — Full codebase audit (verification + repair)

- Full report: `~/workspace/projects/jarvis-audit-report.md`.
- Checked: git sync (local `d7b6dcc` vs origin `614fcd2`, same
  content); full unittest suite; `compileall` (0 failures); import
  of all 57 modules (57 OK); `main.py` smoke run (starts, exits
  cleanly, Ollama connection failure handled gracefully, no crash);
  DB integrity (`integrity_check` ok, 0 orphans, universe 10,619
  with Phase A imports present).
- Real test suites: **129 tests, all OK** (Steps 16–20: 109; 15G/15F:
  20). The other 12 `test_*.py` files are manual scripts, not
  unittest suites — they run live network code and write to the
  production DB on import.
- Defects found and fixed: (D1) `requirements.txt` was missing
  `yfinance` and `pymupdf`, which 6 test files and 5 production
  modules need — both added. (D2) Valuation/watchlist/history
  functions crashed with `no such table` on a database that had
  never had their tables initialised (the valuation monitor crashed
  outright) — every such function now calls its `initialise_*()`
  first; verified against a fresh empty database. (D3) `agents/`,
  `agent/`, `tools/` had no `__init__.py` — empty ones added.
- Audit-caused DB writes (test scripts saving live snapshots) were
  removed again; all row counts verified back to the pre-audit
  state. Note: the DB byte hash differs from the pre-audit hash
  because SQLite rewrites file headers on any write; no post-import
  byte backup existed, so a backup of the restored file was taken
  instead. Logical content is verified identical.
- Open items: rename/convert the 12 script-style test files
  (owner decision); `main.py --help` unsupported (cosmetic);
  Ollama full chat turn untested here (no Ollama on this machine);
  no valuation profile exists for any ticker yet, so the monitor
  returns "no profile" until one is created.
- Verdict: **the program works as intended** after the fixes above.
  Next step remains Step 21 (intrinsic valuation).

## Important project rules

- Do not modify `main.py` before integration approval.
- Do not modify the production database without approval.
- Never delete existing data without approval.
- Do not commit or push without permission.
- Preserve existing working functionality.
- Test before claiming success.
- Protect secrets, private database contents, and generated cache data.
- Do not proceed to the next roadmap step without approval.

## Latest test results

- Syntax checks for `agents/us_issuer_enrichment.py`, `agents/us_market_discovery.py`, and `agents/market_universe.py` using `.venv`: passed.
- Focused temporary-data tests using `.venv` for CLI behavior, read-only SQLite, cache validity and coverage, no-network behavior, no output writes, and fail-closed malformed-cache handling: passed.
- Command `.\.venv\Scripts\python.exe agents\us_issuer_enrichment.py --preflight-only`: passed. Results: 7,625 database listing rows; 6,040 unique CIKs; 3,075 valid cache records; 0 invalid; 0 fresh; 3,075 stale; 2,965 missing; 0 extra.
- Disk check: passed. Free space was 794,104,000,512 bytes; required staging/backup space was 2,992,937,635 bytes.
- Production database SHA-256 remained `812ECB538E3694EA31C467A7621DE8031BE758642EF4B113ECA8F1889E89D4CD` before and after.
- Original cache SHA-256 remained `409217A271B02DC79BF37A3FB21EA98DFCF9D0CFD927B6ADE458C1F7734AD8EC` before and after. Cache size remained 975,276,257 bytes.
- No production enrichment outputs were created or modified. No SEC requests or full enrichment were run.

## Next action

Prioritize the five evidence-quality and issuer-structure improvements recorded in the v3 audit before any production import. Do not import securities into the production market universe or run full-universe enrichment.

## 2026-11-07 — 20-CIK pilot implementation

- Added `--pilot-ciks` and `--pilot-output-dir` to `agents/us_issuer_enrichment.py`. Pilot mode requires both options, exactly 20 distinct normalized CIKs, and confirms each CIK exists in the production database through a read-only connection.
- Pilot processing scopes database selection, cache contents and coverage, journal replay/reporting, mapping refresh, SEC loop, CSV, report, coverage output, and journal/cache writes to the selected CIKs. Cache, journal, CSV, report, and coverage paths are routed to the isolated pilot directory. The directory is rejected if it overlaps the production output tree. Existing staged cache replacement, verified backup, and journal checkpoint behavior remains in use against pilot paths.
- The run prints selected count and planned pilot paths before any SEC requests. Normal invocation without pilot arguments continues through the production constants and full-universe issuer selection.
- Added `test_us_issuer_pilot.py` with temporary SQLite and filesystem fixtures and mocked SEC retrieval. It covers 19/21 CIK rejection, duplicates, unknown CIKs, valid 20-CIK acceptance, output overlap rejection, journal isolation, and preservation of the fixture production database/cache.
- The initial test attempt was blocked by the Codex sandbox's access to the base interpreter; this was not evidence that the user's normal Windows environment was broken. The user subsequently verified the test run in normal PowerShell (details below). The pilot was not run and no live SEC requests were made.
- Files changed for this task: `agents/us_issuer_enrichment.py`, `test_us_issuer_pilot.py`, and this status file. Existing unrelated working-tree changes were left untouched.

### Historical pending note

- This entry preceded the successful 2026-11-08 pilot recorded below; the pilot is now complete and pilot-validated, not production-approved.

## 2026-11-08 — Final 20-CIK pilot audit

- Updated pilot mode to select issuers with `read_only=True`. `get_us_issuers()` opens the SQLite URI with `mode=ro` and `uri=True` for pilot execution; normal execution still uses the original writable connection mode.
- Strengthened `test_us_issuer_pilot.py`: it now asserts the exact 20 mocked SEC CIKs requested, confirms SQLite was opened with `mode=ro`, exercises the actual pilot path resolver, injects out-of-pilot cache/journal records, validates scoped CSV/coverage/report outputs, and compares temporary production database/cache bytes before and after.
- Path resolver test confirms an isolated sibling directory is accepted and production output directory, nested child, and containing parent are rejected.
- Focused command executed with the existing project interpreter: `D:\AiAssistant\.venv\Scripts\python.exe -m unittest test_us_issuer_pilot.py -v`.
- Result: **Ran 3 tests in 0.241s — OK**. All three tests passed: CIK validation, actual path resolution/overlap rejection, and mocked pilot allowlist/read-only/database-cache preservation.
- The sandbox initially denied launching the base interpreter; the explicitly requested test command then ran successfully with elevated sandbox access. SEC retrieval remained mocked. No production DB/cache was touched, and no real pilot or live SEC requests were made.
- The previously identified SQLite write-access risk is resolved for pilot mode. Remaining concern: the no-pilot path continues using the existing writable SQLite connection intentionally; no change to its behavior was made. The pilot remains unrun pending approval.

## 2026-11-08 — Approved pilot preflight stopped

- Requested output directory `D:\AiAssistant\data\market_data\us\pilot_20` did not exist and contained no prior results.
- The requested directory is nested under the production output directory `D:\AiAssistant\data\market_data\us`. The current `pilot_paths()` safety check rejects directories that overlap the production output tree, so the required isolation check fails before pilot execution.
- Stopped without invoking enrichment. No SEC requests were made, no other collection process was started, and no production database or SEC cache was accessed or changed during this preflight. SEC request, success, and failure counts are therefore not applicable.
- To proceed, select and approve an isolated output directory outside the production US output tree; do not bypass the overlap check or fall back to normal enrichment.

## 2026-11-08 — Approved pilot preflight at isolated path (stopped)

- Existing `pilot_paths()` resolver accepted `D:\AiAssistant\data\market_data\us_pilot_20`; every planned pilot path (cache, journal, CSV, report, coverage, and root) was confirmed non-overlapping with the production US output directory and production output files.
- The destination did not exist and had zero entries, so there were no previous pilot results to overwrite.
- Safety check failed at concurrent-process inspection: `Get-CimInstance Win32_Process` returned a `CimException`, so the presence or absence of another SEC collection process could not be confirmed. Stopped immediately without invoking enrichment or checking/reading the production database.
- No SEC requests were made; no pilot outputs were created; the production database and SEC cache were not accessed or modified. The supplied CIKs were not validated against the production database and no database read-only check was performed because execution was stopped at the failed concurrency check.
- Resume only after confirming no other SEC collection process is running, then repeat preflight and validate the approved 20 CIKs before pilot execution.

## 2026-11-08 — Approved isolated 20-CIK pilot executed

- Preflight: the existing path resolver accepted `D:\AiAssistant\data\market_data\us_pilot_20`; the pilot cache, journal, CSV, report, coverage, and output root were each confirmed not to overlap production output paths. The destination was absent/empty before execution.
- The exact 20 approved CIKs were distinct and all 20 matched records in the production database. The validation and enrichment database reads both used SQLite URI `mode=ro`; runtime guard observed two read-only SQLite opens.
- Concurrency check immediately before execution found zero matching SEC collection processes.
- Execution used `D:\AiAssistant\.venv\Scripts\python.exe` to run the pilot CLI arguments only. SEC retrieval attempts: 20; successful CIK responses: 20; failed/unavailable CIKs: 0. HTTP statuses: 20 responses with 200. Retries: 0; `Retry-After` headers: 0; HTTP 429 rate-limit responses: 0; network exceptions: 0.
- Pilot cache records: 20; CSV rows: 20; coverage was matched=20, missing=0, invalid=0, stale=0.
- CSV: `D:\AiAssistant\data\market_data\us_pilot_20\sec_issuer_enrichment.csv`. Report: `D:\AiAssistant\data\market_data\us_pilot_20\sec_issuer_enrichment_report.txt`. Cache and per-CIK journal files were also written under the isolated pilot directory.
- Production database SHA-256 remained `812ECB538E3694EA31C467A7621DE8031BE758642EF4B113ECA8F1889E89D4CD`; production SEC cache SHA-256 remained `409217A271B02DC79BF37A3FB21EA98DFCF9D0CFD927B6ADE458C1F7734AD8EC`. Both were byte-for-byte unchanged.
- No full-universe enrichment, other collection process, `main.py` change, commit, or push occurred. The real pilot completed successfully; broader production validation remains pending review and approval.

## 2026-11-08 — Repeat pilot preflight stopped

- The existing path resolver again confirmed that `D:\AiAssistant\data\market_data\us_pilot_20` and every planned pilot output path are isolated from production output paths.
- The pilot directory contains 25 existing entries, including the prior pilot cache, CSV, coverage, report, and 20 journal records. The no-overwrite preflight therefore failed.
- Stopped before CIK/database and concurrency checks or pilot execution. No SEC requests were made, and no existing pilot or production results were overwritten or modified.

## Step 15G — US security classification design and implementation

### Architecture review

- `agents/us_market_discovery.py` imports SEC `company_tickers_exchange.json` rows into `market_universe` as generic `equity` listings. The SEC submissions enrichment adds issuer-level ticker/exchange metadata; it does not establish the type of each individual security.
- `agents/market_universe.py` stores one row per `(ticker, exchange)` and links rows to an issuer CIK. This preserves multiple symbols for one CIK, but the current `security_type` vocabulary (`equity`, `reit`, `financial`) cannot represent the requested instrument types or distinguish issuer structure from security form. It has no CUSIP/ISIN/share-class fields; the current uniqueness rule cannot represent two records with the same ticker and exchange as separate issues.
- `agents/hkex_security_classifier.py` provides useful candidate/manual-review/report patterns. Its exclude outcomes are unsuitable for US 15G because special securities must be retained. `agents/sg_market_discovery.py` explicitly filters ETFs, warrants, debt, and depositary receipts, so its filter must not be reused.
- The 15G candidate implementation writes CSVs and a report only, leaving the database schema and production records untouched. Any later schema change should be additive, preserve current rows and manual classifications, and receive a separate migration review.

### Classification model

- Keep issuer and instrument as separate concepts. Link securities to CIKs, but never use CIK as a security key or deduplicate listings by CIK.
- Record an issuer/entity kind separately (for example operating company, REIT, ETF/open-end fund, closed-end fund, BDC, UIT, other, unknown) from each listing's security form (common share, ADR/ADS, preferred share, fund share, warrant, unit, rights, debt/ETN, partnership unit, other, unknown). A REIT may issue common and preferred shares; classification must allow both dimensions to be recorded.
- Preserve share class/series labels and stable identifiers where available (CUSIP, ISIN, SEC series/class identifiers), as well as ticker, exchange, and CIK. Keep each listing and share class as a separate candidate; only relate two listings when source evidence supports the relationship.
- Include decision status (`confirmed`, `candidate`, `manual_review`), reason, confidence/evidence strength, source type and reference/accession, as-of date, and classifier version. Unknown or conflicting cases remain in the output with review flags.
- Treat special instruments as retained classifications, not exclusions. Keep “classification” separate from any later valuation eligibility or research suitability decision. Preserve manual decisions; automated evidence must not overwrite them.

### Evidence hierarchy

1. Exchange listing/security directories confirm current symbol, exchange, instrument description, and issue-level identifiers where supplied. Use Nasdaq Trader symbol-directory fields (including its ETP data where available) and NYSE Group listings directories for their covered markets. Verify each source's use/licensing terms before automated collection.
2. SEC filings provide the primary legal description of a class: Form 8-A and offering registration statements/prospectuses (such as S-1/424B and foreign-issuer filings) for class and instrument terms; Form F-6 and related issuer filings for ADRs; N-1A for mutual funds/ETFs; N-2/N-8A and SEC fund series/class data for closed-end funds and BDCs.
3. SEC submissions metadata supplies the issuer CIK, current name, filing history, and issuer-associated tickers/exchanges. Treat those ticker arrays as issuer-level clues only, not as proof that each symbol is the same common share.
4. Issuer investor-relations prospectuses, charter/share-class documents, and exchange listing notices supplement or resolve ambiguous cases. Secondary market-data vendors may help generate review leads but must not be the sole classification source.

### 15G work sequence

- 15G-A Define a documented taxonomy and field dictionary, including how ETFs, ADRs, preferred classes, REIT issuers, registered funds, warrants, units, rights, and uncertain issues are represented.
- 15G-B Build a source-backed candidate file from existing US listing/issuer records and enrichment, preserving raw values and one row per distinct listing/class candidate.
- 15G-C Apply conservative, evidence-linked rules; route ambiguous or conflicting records to manual review rather than assuming common stock.
- 15G-D Produce a classification CSV and coverage/review report with counts, duplicate/relationship checks, missing identifiers, and all manual-review reasons. Do not write to `assistant.db`.
- 15G-E Validate against temporary fixtures covering multiple classes under one CIK, one issuer with multiple unrelated instruments, ADRs, funds, REIT common/preferred shares, warrants/units, missing identifiers, and conflicting sources. Preserve every candidate in the output.
- After 15G review, continue to 15H validation/normalization. Any production universe import remains Step 15I and requires a separately reviewed, approved change.

### Required primary sources

- SEC EDGAR access and ticker/exchange association datasets: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- SEC Form 8-A (class registration/listing) and Form S-1 security-class description: https://www.sec.gov/files/form8a.pdf and https://www.sec.gov/about/forms/forms-1.pdf
- SEC fund disclosure forms (N-1A, N-2) and closed-end-fund/series-class data: https://www.sec.gov/about/divisions-offices/division-investment-management/fund-disclosure-glance/disclosure-reference and https://www.sec.gov/data-research/sec-markets-data/closed-end-fund-information
- SEC Form F-6 and ADR guidance: https://www.sec.gov/about/forms/formf-6.html
- Nasdaq Trader combined symbol directory: https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt; field definitions: https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
- NYSE public Listings Directory: https://www.nyse.com/listings_directory/stock; category query endpoint used by its UI: https://www.nyse.com/api/quotes/filter; NYSE Group security type definitions: https://www.nyse.com/publicdocs/nyse/data/NYSE_Group_Security_Master_Client_Specification_v4.0.6.pdf
- NYSE listings and listing notices: https://www.nyse.com/listings_directory/etf and https://www.nyse.com/market-data/corporate-actions

- Status: **candidate and exchange-evidence collection implemented; initial source-backed candidates generated; human/source review remains**. No securities have been imported and no production schema changes have been made.

### 2026-11-08 — Step 15G candidate pipeline implementation and run

- Added `agents/us_security_classifier.py` with separate issuer-structure and security-form taxonomies. It preserves each US market-universe listing as an independent row, including multiple tickers/share classes under one CIK and special-security candidates.
- Evidence input is a local CSV keyed by exact ticker, exchange, and CIK. Primary references must be HTTPS links to SEC domains for SEC evidence or official Nasdaq/NYSE domains for exchange evidence; issuer IR and secondary evidence only provide suggestions. Missing or conflicting primary evidence stays `unknown` and `manual_review`.
- Candidate fields include listing identity, share class, classification status/confidence, evidence source/reference, rationale, classification date, and manual-review status/reason. Name/ticker heuristics are recorded only as review hints.
- Added output-path protection against overlap with production US/HKEX data trees, the 20-CIK pilot directory, or the production SQLite database. Outputs refuse to overwrite existing files. The production database is opened using SQLite URI `mode=ro`.
- Added `test_us_security_classifier.py` using temporary SQLite/filesystem fixtures. It covers independent listings and share classes, primary evidence, secondary-only evidence, conflicting evidence, invalid evidence, read-only DB byte preservation, no-overwrite behavior, and output path overlap rejection.
- Focused command: `D:\AiAssistant\.venv\Scripts\python.exe -m unittest test_us_security_classifier.py -v` — **Ran 7 tests in 0.049s — OK** (final run after official-source URL validation and path-safety updates).
- Candidate generation command: `D:\AiAssistant\.venv\Scripts\python.exe agents\us_security_classifier.py` — completed without network access. Evaluated 7,625 listings (6,040 CIKs), retained 7,625 candidate rows, and wrote 7,625 manual-review rows; confirmed=0. No primary evidence CSV was present, so every classification remains unknown/manual review. Name/ticker hints include 48 possible ETFs, 2 possible depositary shares, 15 possible preferred shares, 21 possible REIT issuers, 59 warrant-suffix leads, and 50 unit-suffix leads; these are unverified leads, not counts of confirmed instruments.
- Outputs: `data/market_data/us_security_classification/us_security_classification_candidates.csv`, `us_security_classification_manual_review.csv`, and `us_security_classification_report.txt`. They were generated in a new sibling directory, separate from production `data/market_data/us` and the existing `data/market_data/us_pilot_20`; overwrite protection is active.
- Production database SHA-256 before and after: `812ECB538E3694EA31C467A7621DE8031BE758642EF4B113ECA8F1889E89D4CD`. Production SEC cache SHA-256 before and after: `409217A271B02DC79BF37A3FB21EA98DFCF9D0CFD927B6ADE458C1F7734AD8EC`. Both remained byte-for-byte unchanged. No SEC requests, pilot, database import, `main.py` change, commit, or push occurred.
- Remaining limitation: the candidate pipeline is implemented and tested, but evidence-backed classification is not complete until official issue-level SEC/exchange evidence is collected and reviewed. No automated classification should yet feed the production universe or financial-analysis engine.

### 2026-11-08 — Step 15G Phase 2 official issue-level evidence collection

- Local inventory review found no Nasdaq or NYSE listing-directory snapshots. Existing US discovery records derive from SEC `company_tickers_exchange.json`; the issuer enrichment cache and 20-CIK pilot outputs are issuer-level and do not supply per-listing security form. Existing Step 15G v1 outputs were preserved.
- Verified live accessibility of official exchange sources. Nasdaq Trader's combined `nasdaqtraded.txt` provides symbol, security name, listing exchange, ETF flag, test-issue flag and other directory fields; NYSE's public Listings Directory exposes categories for equity, ETF, preferred stock, warrants, rights, trusts, bonds and REITs. Nasdaq and NYSE source field descriptions and directory endpoints are documented in the preceding source section.
- Added `agents/us_security_evidence.py`: timestamped/content-hash snapshots, source manifest and attribution, 24-hour cache reuse, one-second minimum request interval, bounded retries/backoff and `Retry-After` support. The NYSE source response includes some non-NYSE MIC records; the final join accepts only `XNYS` for NYSE evidence. All sources join by exact ticker plus exchange; CIK is optional evidence metadata and is only checked for contradiction.
- Added conservative parsers for explicit exchange `ETF=Y`, NYSE directory instrument categories, explicit issue-form descriptors (common/ordinary, American depositary, preferred, warrant, right, unit, debt), and class/series labels. A known security form without known issuer structure is provisional. Names are read only from the exchange issue-description field; issuer company names and ticker suffix heuristics do not establish classification. Conflicting evidence is a separate status and remains under review.
- Final focused command: `D:\AiAssistant\.venv\Scripts\python.exe -m unittest test_us_security_classifier.py test_us_security_evidence.py -v` — **Ran 13 tests in 0.054s — OK**. Coverage includes temporary SQLite/filesystem fixtures, mocked Nasdaq/NYSE HTTP responses, rate-limit retry handling, cache metadata/reuse, exact ticker-and-exchange joins, optional/mismatched CIK handling, and representative common, ETF, ADR, preferred, REIT, warrant, conflicting, provisional and unknown cases.
- Collector command: `D:\AiAssistant\.venv\Scripts\python.exe agents\us_security_evidence.py`. It used official Nasdaq Trader and NYSE directory snapshots, then reused the cached snapshots for the final venue-corrected run. No SEC requests were made. The production database remained read-only; no securities were imported.
- Final output directory: `data/market_data/us_security_classification_v3/` containing `official_issue_level_evidence.csv`, candidate CSV, manual-review CSV, report and `source_manifest.json`. Final result: **7,625 listings; 137 confirmed; 6,120 provisional; 8 conflicting; 1,360 unknown; 7,488 require manual review**. 9,008 exact listing evidence rows matched by ticker and exchange; 6,643 distinct listing keys received primary evidence. Common shares, ADRs, preferred shares, warrants, units, rights, debt notes, ETFs, and REIT issuers are represented. Example rows: AAPL (common share, provisional), BABA (ADR, provisional), ACGLN (preferred Series G, provisional), AACIW (warrant, provisional), BITA (ETF, confirmed), AAT (REIT common share, confirmed).
- The first v2 draft is retained with `SUPERSEDED_NOTICE.txt`: it joined NYSE results for non-NYSE MICs. Version 3 corrects the venue filter and is the review output. Version 1 outputs are untouched.
- Production DB SHA-256 stayed `812ECB538E3694EA31C467A7621DE8031BE758642EF4B113ECA8F1889E89D4CD`; SEC cache SHA-256 stayed `409217A271B02DC79BF37A3FB21EA98DFCF9D0CFD927B6ADE458C1F7734AD8EC`. Existing v1 classification output tree hash stayed `9C12B50961D7CE54BE29B2630A5B38B15E32AE445851DDC7CC3F4F341329DCE3`; pilot output tree hash stayed `8EDC4A11B556E8F9CE2AFED3E68A92822E7BC5FF06C728309BFF2025CA496E8A`.
- Remaining limits: many listings have only one proven dimension or no mapped source record; these remain provisional/unknown. The SEC evidence input path is supported, but this Phase 2 run did not collect individual SEC filings. Classification is not production-approved and must not feed the production universe or financial-analysis engine before review.

### 2026-11-08 — Read-only audit of Step 15G v3 results

- Scope: read `us_security_classification_candidates.csv`, `official_issue_level_evidence.csv`, `source_manifest.json`, the v3 report, and the cached official Nasdaq/NYSE snapshots named by the manifest. No data was collected or written; no SEC requests or production database/cache access occurred. Only this status file was updated.

#### Numerical breakdown

| Status | Listings | Issuer structure known / unknown | Security form known / unknown | NASDAQ / NYSE | Main reason |
|---|---:|---:|---:|---:|---|
| Provisional | 6,120 | 14 / 6,106 | 6,106 / 14 | 4,209 / 1,911 | 6,106 have a security form but no issuer structure; 14 are NYSE REIT issuers without a confirmed form. |
| Unknown | 1,360 | 0 / 1,360 | 0 / 1,360 | 124 / 1,236 | 982 have no exact ticker/exchange evidence; 378 have source rows but no recognized structure or form. |

- Provisional reasons: `Issuer structure is unconfirmed` = 6,106; `Security form is unconfirmed` = 14.
- Unknown reasons: 838 have no exact evidence and no candidate hint; 144 have no exact evidence plus a name/ticker hint; 378 have matched evidence but both classification axes remain unknown. Hints never confirm a class.
- Source presence is non-exclusive. For provisional listings: Nasdaq Trader = 6,120; NYSE `EQUITY` = 1,813; NYSE `PREFERRED_STOCK` = 19; NYSE `REIT` = 14; NYSE `TRUST` = 9. For the 378 unknowns with evidence: Nasdaq Trader = 378 and NYSE `EQUITY` = 281.
- The 6,106 provisional known forms break down as: common shares 4,800; warrants 359; units 284; ADR/ADS 273; debt notes 160; preferred shares 127; rights 103. The counts sum to 6,106. Issuer structure is unknown for this entire group.
- The 982 unknown listings without an exact mapped source are all marked active in the candidate data and carry a CIK. In the cached official snapshots, exact ticker strings for 412 appear under a different MIC: 271 under XASE, 140 under ARCX, and HLT under XNGS. The latter also appears in Nasdaq Trader as NASDAQ-listed. CQS/Nasdaq-symbol alias fields did not produce exact ticker aliases for the remaining no-source records. The other 570 no-source listings have no exact symbol in the cached snapshots.
- Existing official evidence already confirms 29 ETF/ETP listings and 108 REIT common-share listings. Fourteen more REIT issuer structures are known but their forms are not. One ETF-tagged listing, IPB, is conflicting. The categories represented among provisional listings are direct issue forms only; they do not establish the unknown issuer structures.
- In the 378 evidence-bearing unknowns, Nasdaq issue-description text contains non-exclusive leads for 42 ADR/ADS/depository labels, 153 common/ordinary-share phrases (4 overlap with ADR/ADS descriptions), 32 closed-end-fund labels, 17 subordinate-voting-share labels, 6 preferred labels, 2 REIT labels, 1 BDC label, and 1 warrant/ADW label. These are raw official-description pattern hits, not confirmed classifications; ambiguous wording such as an underlying share inside an ADR must remain distinct from the listed security form.

#### Eight conflicts inspected against cached raw source descriptions

| Listing | Conflicting evidence | Read-only audit finding |
|---|---|---|
| BSBR (NYSE) | Nasdaq says ADR; NYSE text says sponsored ADR representing one unit, with common and preferred components. | Likely parser artifact: `unit` describes the ADR's underlying interest, while the listed wrapper is an ADR. |
| IPB (NYSE) | Nasdaq `ETF=Y`; NYSE `PREFERRED_STOCK` category and description says preferred certificates. | Direct source disagreement. Keep conflicting pending authoritative issue-level resolution. |
| MHNC (NYSE) | Nasdaq describes 7.75% notes due 2043; NYSE marks it preferred and describes PFD. | Notes-versus-preferred conflict; keep manual review. |
| RNP (NYSE) | Nasdaq says common shares; NYSE title contains “REIT & PFD & INCM FD,” which the parser treated as preferred. | Likely parser artifact: `PFD` describes the fund's investment focus, not necessarily this listing's form. |
| TPC (NYSE) | Nasdaq `TPC` is Tutor Perini common stock; NYSE `TpC` is AT&T preferred Series C. | Definite ticker-key collision caused by uppercasing case-sensitive NYSE symbols. The two listings must not be joined. |
| TVC (NYSE) | Nasdaq says common stock; NYSE text/category describes TVA preferred/power bonds. | Direct source disagreement; keep manual review. |
| VBF (NYSE) | Nasdaq says Invesco Bond Fund common stock; NYSE title says “INVESCO BOND FUND,” which the parser treated as debt. | Likely parser artifact: “Bond Fund” is an issuer/fund name, not proof that this ticker is a bond. |
| WDH (NYSE) | Nasdaq ADS description specifies Class A ordinary shares; NYSE ADS text abbreviates the underlying as “CL ORD,” parsed as class ORD. | Likely share-class parsing artifact; both descriptions identify ADSs, but the underlying class label needs normalization. |

- Five conflicts (BSBR, RNP, TPC, VBF, WDH) appear likely to be parsing/key-normalization artifacts. IPB, MHNC, and TVC remain substantive source disagreements on current evidence.

#### Five highest-impact opportunities

1. Resolve issuer structure for the 6,106 provisional known-form listings using freshness-checked official issuer-level SEC evidence where available. CIK may link issuer structure only; it must not establish security form. Do not default the remainder to `operating_company`.
2. Interpret explicit fields already present in cached Nasdaq issue descriptions. Conservative source-specific rules can capture ADR/ADS, closed-end fund, BDC/REIT, subordinate-voting share, and qualified common/ordinary-share labels. Keep single-axis results provisional and preserve the raw descriptor.
3. Reconcile the 412 ticker matches with different MICs before classification. The cached NYSE source includes 89 of the ARCX matches in its ETF category and 3 XASE matches in its REIT category; correct venue identity is required before attaching these records to the database's generic `NYSE` exchange value.
4. Correct the five likely conflict artifacts with case-preserving ticker identity and context-sensitive instrument parsing. Keep IPB, MHNC, and TVC conflicting until their official issue descriptions agree.
5. Triage the remaining 570 no-source listings as a single source-coverage cohort, with the 144 hinted cases prioritized. Keep unknown when no exact current official listing evidence exists; do not manually classify all 7,625 records.

#### Recommended implementation sequence

1. Add regression cases for all eight conflicts; fix case-sensitive ticker joins and scope descriptor rules to the issue-form field/context.
2. Produce a non-production MIC reconciliation view for the 412 mismatches; preserve the source MIC and do not write exchange changes into `market_universe`.
3. Extend parsing for explicit Nasdaq/NYSE descriptions found in the existing snapshots; output only the dimensions directly supported and leave incomplete cases provisional.
4. Validate freshness/provenance of existing SEC issuer metadata, then use it only for issuer structure on the 6,106 provisional listings. No security form should be inferred from CIK or company name.
5. Retain the 570 unrepresented listings and the three substantive conflicts as grouped review cohorts; do not start a universe-wide manual review or production import.

### 2026-11-08 — Step 15G Phase 3 identity and existing-evidence corrections

- Implemented case-preserving ticker matching in `agents/us_security_classifier.py`. Exchange names are still normalized, while ticker case is retained in database, evidence and NYSE URL keys. This keeps NYSE `TPC` (Tutor Perini) distinct from `TpC` (AT&T preferred Series C).
- Improved explicit issue-description parsing in `agents/us_security_evidence.py`: ADR/ADS wrapper descriptions take precedence over descriptions of underlying components; common-share suffixes take precedence over issuer-name words such as “Bond”; preferred shorthand is accepted only in explicit security/series contexts; and underlying `CL ORD` is no longer recorded as a security share class. Issuer/company names and ticker patterns remain non-confirming.
- Added `agents/us_security_classification_phase3.py`. It reads only the v3 source manifest's cached snapshots, verifies each content hash, reads the production market-universe database with SQLite URI `mode=ro`, and issues no requests. It rebuilds issue-level evidence with exact case-sensitive ticker plus exchange matching, retains one-axis evidence as provisional, and writes an XASE/ARCX/XNGS MIC reconciliation view without mapping those MICs into the database exchange field.
- Added `test_us_security_phase3.py`, including regression coverage for all eight v3 conflicts and a temporary-database end-to-end rebuild using temporary cached snapshots. Focused command: `D:\AiAssistant\.venv\Scripts\python.exe -m unittest test_us_security_classifier test_us_security_evidence test_us_security_phase3` — **Ran 17 tests — OK**. Tests use no live SEC or exchange requests.
- Final output: `data/market_data/us_security_classification_v4_2/`. It contains the evidence CSV, all-listings candidate and manual-review CSVs, report, source manifest, MIC reconciliation CSV, a 196-row per-listing v3-to-Phase-3 change CSV, and a comparison/conflict review report. The earlier same-task draft directories `us_security_classification_v4/` and `us_security_classification_v4_1/` are marked as superseded; use v4_2.
- All 7,625 listing rows were retained. v3 -> v4_2 counts: confirmed **137 -> 137**; provisional **6,120 -> 6,116**; conflicting **8 -> 37**; unknown **1,360 -> 1,335**. The change CSV records each changed listing, its old/new status, structure, form, share class, evidence source and rationale.
- All five audited artifacts now remain as provisional one-axis classifications: BSBR=ADR/ADS; RNP=common shares; TPC=common shares; VBF=common shares; WDH=ADR/ADS with underlying Class A. They are not fully confirmed because issuer structure is still unknown. IPB (ETF/preferred), MHNC (notes/preferred), and TVC (common/debt-preferred) remain conflicting and in manual review.
- Explicit parsing exposed **34 additional** Nasdaq/NYSE security-form disagreements, which remain conflicting/manual-review rather than being reconciled by guessing. Nine previously provisional records became unknown where v3's broad text rules had accepted fund-name/abbreviation terms without an explicit listed-security descriptor. Thirty-four formerly unknown records became provisional when an explicit issue form was parsed; issuer structure remains unknown. The per-listing change CSV details these and all other material changes.
- The MIC reconciliation view contains **4,582** source rows: XASE 327, ARCX 2,752, and XNGS 1,503. These are review candidates only and are not attached to US listings as classification evidence.
- The v3 candidates, evidence, report and manifest remain untouched; their SHA-256 values were checked after generation. Cached snapshots matched the hashes in the v3 manifest. No database writes, schema changes, cache writes, imports, SEC/exchange requests, `main.py` changes, commits or pushes occurred.
- Remaining limitation: security form is improved where the cached exchange issue descriptions explicitly identify the listed security, but issuer structure remains unknown for most rows. Phase 3 is a candidate-classification improvement, not production approval; review the new explicit conflicts and decide separately whether targeted issuer-level SEC evidence collection is worthwhile.


### 2026-11-08 — Step 31 market news collection (PARTIAL, not complete)

- New module: `agents/news_collection.py`; tests:
  `test_news_collection.py` — **29 tests, OK** (439 total
  with Steps 16–30, `python -m unittest discover`, run from
  the project root with
  `~/workspace/projects/jarvis-venv/bin/python`, no
  regressions). Full report:
  `~/workspace/projects/step31-report.md`. Production DB
  SHA-256 verified unchanged before and after
  (`36862922…089187`, post-audit hash). There is no news
  table in the database and none was created.
- Collection only: fetch, dedupe, store, tag. No
  summarising, no sentiment, no recommendations (Steps
  32/33). No buy/sell call anywhere. The collector takes an
  explicit ticker list — the whole universe is never fetched
  in one run — with a caller-supplied per-ticker cap
  (default 5) and delay between fetches (default 1.0 s,
  injectable sleep). The fetcher is pluggable, so tests use
  fixture fetchers and never touch the network.
- **Default fetcher**: the ddgs news endpoint first, falling
  back to the existing `tools/research.py::search_web` text
  search. Same ddgs library the project already depends on;
  no new dependency, no second searcher. The switch was
  driven by a live finding (below), not by preference.
- Per-article record: title, source (publisher name when
  the fetcher supplies one, else the URL domain),
  published_date (raw) + published_date_iso (parsed,
  None when unparseable), url, snippet, tickers tagged,
  fetched_at (UTC), query. The query ticker is always
  tagged; other requested tickers (or company-name aliases
  via an optional `names` mapping) are tagged on whole-word
  mention. Dedupe is by normalised URL, else lowercased
  title + source; a duplicate's ticker tags merge into the
  kept article.
- Storage is JSON files, one per run, under a
  caller-chosen directory (default `data/news/` — the same
  file-log pattern Step 26 uses for
  `data/detection_runs.jsonl` and Step 30 uses for
  `data/theses/`). Each run file carries the fetch
  parameters, per-ticker counts and errors, and the deduped
  articles. `list_articles(ticker=None, since=None)` reads
  all run files, dedupes across runs, newest published
  date first; corrupt run files are skipped, not fatal.
- **Live collection, MSFT, cap 5 — two runs.** The first
  run (text search only) returned 5 aggregator hub pages
  with no dates (stockanalysis.com MSFT page, Seeking Alpha
  news/analysis hubs, wallstreetnumbers price history,
  Reuters company page) — a finding about the text endpoint,
  kept as run file `20261108_163835_MSFT.json`. The second
  run (news endpoint) returned 5 real dated articles
  (`20261108_164957_MSFT.json`): Investopedia 2026-10-28
  (expected move after earnings), Barchart 2026-10-28
  (buyouts to 60,000 employees), Blockonomi 2026-11-05
  (Polen Capital AI growth), TheStreet 2026-10-29 (Q1 FY2027
  earnings call), U.S. News 2026-04-22 (buy-or-not explainer).
  The news endpoint is not recency-filtered (the April
  article proves it), so `since` filtering on read matters;
  headlines are stored, not endorsed. Stored total for MSFT
  after both runs: 10 items. Network note: the text path
  showed intermittent DNS failures immediately after a
  successful call; the news-first / text-fallback chain and
  per-ticker error recording are the guards.
- Bugs found and fixed during testing: (1) dedupe dropped
  the second ticker's tag — duplicates now merge tags;
  (2) two runs in the same second collided on filename and
  the second overwrote the first — filenames now get a
  `_2`, `_3` suffix.
- **Step 31 is PARTIAL, not complete.** Live collection has
  run for MSFT only; no other ticker has ever been
  collected. No sentiment, summarising, or linking of
  articles to theses/candidates (Steps 32–34). No
  scheduling (Step 46), no wiring into `main.py` (Step 50).
  The stored hub pages from the first run are not articles
  and nothing filters them out yet. The `data/news/` run
  files are untracked generated data; whether they belong
  in Git is undecided. Sufficient to start Step 32 (daily
  market summaries).
