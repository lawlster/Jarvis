# Jarvis Project Status

## Current development position
- Current step: 16 — Unified financial data (PARTIAL — US/SEC standardisation built and tested 2026-11-08; SG/HK not yet real — see Step 16 section below)
- Current subtask: Step 17 financial ratios on US data; Phase A close-out (15H/15I/15J + 14H/14I decisions pending owner) remains gated on owner decisions
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

