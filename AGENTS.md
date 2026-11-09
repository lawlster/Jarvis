# AGENTS.md — Jarvis Project Instructions

## 1. Project Overview

Project name: Jarvis  
Project directory: `D:\AiAssistant`

Jarvis is a personal AI assistant and financial research platform.

The long-term objective is to build a capable, continuously running AI assistant that combines:

- Local and cloud AI models
- Financial research and value-investing analysis
- Global market discovery and monitoring
- Natural-language conversations
- Long-term memory and contextual understanding
- Web research and external data collection
- Tool usage and automation
- Voice interaction
- Computer and application interaction

The project is being developed incrementally. Preserve the existing architecture and functionality.

## 2. Critical Development Rules

These rules must be followed for every task.

### Before modifying files

1. Inspect the relevant existing files first.
2. Understand how the existing implementation works.
3. Check dependencies and database interactions.
4. Identify potential side effects before making changes.
5. Explain the proposed changes before implementing substantial modifications.

### File modification rules

1. Preserve existing functionality.
2. Do not unnecessarily rewrite working modules.
3. Prefer complete, coherent file implementations over scattered patches.
4. Do not modify unrelated files.
5. Do not create duplicate modules when an existing module can be extended safely.
6. Do not modify `main.py` until the relevant modules are ready and integration is explicitly approved.
7. Do not delete existing database records.
8. Do not perform destructive database migrations without explicit approval.
9. Do not overwrite manually curated classifications with automatically collected data.
10. Do not assume a module works simply because it exists.

### Testing rules

1. Test changes using the smallest meaningful set of tests.
2. Avoid excessive repetitive testing.
3. Validate real outputs, not just successful execution.
4. Report errors honestly.
5. Do not claim a feature is complete until it has been validated.
6. Clearly distinguish implemented, partially tested and fully validated functionality.

### Communication rules

1. Explain the purpose of a task before implementing it.
2. Work in manageable steps.
3. Report the exact files created or modified.
4. Provide exact commands when user action is required.
5. Do not silently change project architecture.
6. Do not proceed to the next major roadmap step without approval.
7. Keep explanations concise and practical.

## 3. Existing Technical Environment

- Operating system: Windows
- Development environment: Visual Studio Code
- Project language: Python
- Python virtual environment: `.venv`
- Local AI runtime: Ollama
- Database: SQLite
- Main database: `database/assistant.db`
- Version control: Git
- GitHub repository: `https://github.com/lawlster/Jarvis.git`

Use the existing virtual environment when running project commands.

PowerShell example:

```powershell
cd D:\AiAssistant
.\.venv\Scripts\Activate.ps1
```

## 4. Existing Project Architecture

Important directories:

- `agent/` — Core assistant architecture, brain, planner and memory.
- `agents/` — Financial, market discovery, valuation and research agents.
- `data/financial_statements/` — Financial statement collection and database.
- `data/market_data/` — Market discovery datasets and outputs.
- `data/providers/` — External financial data providers.
- `database/` — SQLite database.
- `reports/` — Generated research reports.
- `tools/` — Calculator, finance, research and valuation tools.
- `main.py` — Main application entry point.

Important existing modules include:

- `agents/market_universe.py`
- `agents/us_market_discovery.py`
- `agents/sg_market_discovery.py`
- `agents/hkex_market_discovery.py`
- `agents/hkex_security_classifier.py`
- `agents/hkex_universe_validator.py`
- `agents/hkex_universe_normalizer.py`
- `agents/us_issuer_enrichment.py`
- `data/financial_statements/statement_database.py`
- `data/financial_statements/sec_collector.py`
- `data/financial_statements/sgx_collector.py`

Inspect actual files before relying on this description.

## 5. Financial Research Philosophy

Jarvis is primarily an educational financial research and value-investing analysis platform.

Its research should consider:

- Business quality
- Competitive advantages and economic moats
- Owner earnings
- Free cash flow
- Return on invested capital
- Balance sheet strength
- Financial consistency
- Historical growth
- Intrinsic value
- Margin of safety
- Capital allocation
- Industry conditions
- Management and governance risks

Use appropriate valuation methods for different industries.

Examples:

- Banks: financial-sector valuation methods.
- Insurers: insurance-specific financial analysis.
- REITs: NAV, AFFO and property-related metrics.
- Operating companies: DCF and appropriate relative valuation.

Do not force a company to qualify if the evidence is insufficient.

Flag missing data, uncertainty, liquidity concerns and data quality issues.

The system is for research and analysis, not automated trade execution.

## 6. Global Market Coverage

The intended coverage includes:

- United States
- Singapore
- Hong Kong
- Other international markets in later stages

Include companies of different sizes, including small and micro-cap companies.

Do not automatically exclude a company solely because of its size.

Instead, flag relevant risks such as:

- Low liquidity
- Governance concerns
- Financial instability
- Volatility
- Insufficient disclosures
- Poor data quality

Security classification must distinguish operating companies from ETFs, funds, warrants, derivatives, preferred securities, depositary receipts, REITs and other special instruments.

Do not deduplicate legitimate securities merely because they share an issuer CIK.

Preserve issuer, security and listing relationships.

## 7. Data Collection and Database Rules

The project uses SQLite.

Before changing database structures:

1. Inspect the existing schema.
2. Understand existing records.
3. Design migrations carefully.
4. Preserve existing data.
5. Explain the migration.
6. Obtain approval before destructive changes.

Data collection should preserve:

- Original source
- Collection timestamp
- Reporting period
- Currency
- Units
- Consolidation basis
- Filing or document references
- Missing-data indicators
- Validation status
- Data provenance

Prefer official sources where possible:

- SEC EDGAR
- HKEX
- SGX
- Company investor-relations websites

Yahoo Finance and other unofficial providers must not be treated as the sole source of truth.

Do not claim complete market coverage without evidence.

## 8. Security and Privacy

Never commit or expose:

- `.env`
- API keys
- Passwords
- Access tokens
- Private credentials
- Personal database contents
- Unnecessary generated cache files

Keep large generated datasets and temporary files out of Git unless explicitly required.

The local SQLite database should remain protected.

Do not upload sensitive information to external services without explicit approval.

## 9. Git and GitHub

GitHub repository:

`https://github.com/lawlster/Jarvis.git`

Before committing:

1. Inspect `git status`.
2. Check staged files.
3. Ensure secrets and private data are excluded.
4. Review changes before committing.

Do not automatically push changes without approval.

## 10. Complete Development Roadmap

This is the master chronological roadmap. Update statuses only when work has actually been completed and validated.

### Part 1 — Foundation and AI assistant

1. Create AiAssistant project and folder structure — DONE
2. Set up Python, VS Code and virtual environment — DONE
3. Set up Ollama and local AI model — DONE
4. Establish initial AI conversation and response — DONE
5. Implement conversation memory and persistence — DONE
6. Set up SQLite database and initial tables — DONE
7. Establish initial assistant agent architecture — PARTIAL
8. Set up OpenClaw integration — TROUBLESHOOTING

### Part 2 — Financial research infrastructure

9. Establish financial research modules — IMPLEMENTED
10. Build watchlist and financial tracking foundation — PARTIAL
11. Establish financial statement database and collection architecture — DONE

Step 11 components:

- 11A Common collection architecture — DONE
- 11B Financial statement database — DONE
- 11C US SEC financial collector — IMPLEMENTED, LIMITED TESTING
- 11D SGX financial collector — PAUSED, REDESIGN REQUIRED
- 11D2 HKEX discovery and financial collector — DISCOVERY PROGRESSING
- 11E Historical financial retrieval — NOT STARTED
- 11F Financial data standardisation — NOT STARTED
- 11G Financial data validation — NOT STARTED
- 11H Automated universe collection — NOT STARTED
- 11I Collection tracking and reliability — NOT STARTED

### Part 3 — Global market discovery

12. Build market-universe database and management module — IMPLEMENTED
13. Build Singapore market discovery — IMPLEMENTED, NEEDS VALIDATION
14. Build and validate Hong Kong market discovery — DISCOVERY/NORMALIZATION DONE
15. Build and validate US market discovery — IN PROGRESS

#### Step 14 — Hong Kong

- 14A Download official HKEX securities list — DONE
- 14B Extract all 17,309 securities — DONE
- 14C Classify securities — DONE
- 14D Validate securities and identifiers — INITIAL VALIDATION DONE
- 14E Investigate missing and duplicate ISINs — INITIAL REVIEW DONE
- 14F Identify RMB secondary counters — 24 IDENTIFIED
- 14G Build normalized HKEX output — DONE
- 14H Review investment companies and depositary receipts — CLASSIFICATION DECISIONS PENDING
- 14I Import approved HKEX securities into market universe — DONE (2026-11-08: 2,793 imported, 2,782 equity + 11 REITs, country 'Hong Kong'; 29 manual-review rows not imported)

#### Step 15 — United States

- 15A SEC ticker/exchange discovery — DONE
- 15B Inspect existing US database records — DONE
- 15C Audit CIK coverage — DONE
- 15D Investigate multiple tickers per issuer — DONE
- 15E Audit ticker suffix patterns — DONE
- 15F Build SEC issuer enrichment module — PILOT-VALIDATED (20-CIK pilot 20/20, 2026-11-08; full-universe enrichment NOT authorised)
- 15G Classify US securities — PHASE 3 COMPLETE, SUFFICIENT TO MOVE ON (v4_2: 137 confirmed, 6,116 provisional, 37 conflicting, 1,335 unknown; issuer-structure gap remains, see PROJECT_STATUS.md 2026-11-08)
- 15H Validate and normalize US securities — DONE (2026-11-08: v3 audit + Phase 3 + import verification; 5,189 core rows matched on ticker+exchange, 0 unmatched)
- 15I Review ETFs, ADRs, funds, preferred shares and REITs — PENDING (1,050 special-form rows held unclassified in the DB as generic 'equity'; 1,372 unknown/conflicting rows also held)
- 15J Import approved US universe records — DONE (2026-11-08: core scope only — 5,189 rows updated in place, 4,874 common_stock + 315 adr, 0 new rows, 0 deletions; total universe now 10,619 rows)

US discovery currently contains 7,625 ticker records and 6,040 unique CIKs.

Do not deduplicate by CIK.

### Part 4 — Financial intelligence engine

16. Build unified financial-data standard — PARTIAL (2026-11-08: US/SEC standardiser built and tested, 21 tests OK; SG/HK mappings are candidate tables only — see PROJECT_STATUS.md Step 16 section)
17. Build financial ratios and metrics engine — PARTIAL (2026-11-08: ratios engine built and tested, 21 tests OK; MSFT only, price ratios rest on a stale 2026-11-03 snapshot; EV/EBITDA and interest coverage unavailable — see PROJECT_STATUS.md Step 17 section)
18. Build financial health and quality analysis — PARTIAL (2026-11-08: health engine built and tested, 11 tests OK, 53 total; MSFT FY2026 verdict strong; Altman Z-score unavailable — retained earnings not in Step 16 set; MSFT only — see PROJECT_STATUS.md Step 18 section)
19. Build historical financial growth analysis — PARTIAL (2026-11-08: YoY/CAGR/consistency/cyclicality built and tested, 35 tests OK; MSFT only — see PROJECT_STATUS.md Step 19 section)
20. Build cash flow and owner earnings analysis — PARTIAL (2026-11-08: cash-flow engine built and tested, 21 tests OK; owner earnings UNAVAILABLE — no D&A in Step 16 concept set — see PROJECT_STATUS.md Step 20 section)
21. Build intrinsic valuation engine — PARTIAL (2026-11-08: 10y DCF engine built and tested, 26 tests OK; MSFT only, owner earnings / EV/EBITDA / historical P/E unavailable — see PROJECT_STATUS.md Step 21 section)
22. Build sector-specific valuation models — PARTIAL (2026-11-08: sector router + bank/insurer/REIT/tech models built and tested, 37 tests OK; only the MSFT technology path is validated on real data — bank/insurer/REIT models are fixture-only, no statements exist for them — see PROJECT_STATUS.md Step 22 section)
23. Build margin-of-safety and relative valuation analysis — PARTIAL (2026-11-08: margin engine, entry-price ladder, tier logic with the owner's 20%-below-conservative strongest-alert rule, deterioration flag wired to Steps 18/19/20, WACC/growth sensitivity and relative P/E cross-check built and tested, 36 tests OK; MSFT only, tier overvalued at $507.12, alert threshold $78.82 — see PROJECT_STATUS.md Step 23 section)
24. Build competitive advantage and business-quality analysis — PARTIAL (2026-11-08: seven-component quality engine built and tested, 15 tests OK; MSFT FY2026 rating excellent, mean score 2.0; quality gate wired into Step 23 tier logic — a weak/insufficient-data rating blocks a strongest alert; moat conclusion qualified as financial evidence only; MSFT only — see PROJECT_STATUS.md Step 24 section)

### Part 5 — Market scanner and research

25. Build global market scanner — PARTIAL (2026-11-08: scanner built and tested, 25 tests OK; runs the full 10,619-row universe in ~1.1s but only 1 ticker is scannable (MSFT, overvalued, quality excellent) — 10,617 have no statements, D05 has no price; ranking with strongest-alert first and fresh-name rotation, fixture-proven alert and weak-business block; dual-listing cache bug found and fixed — see PROJECT_STATUS.md Step 25 section)
26. Build undervalued-company detection — PARTIAL (2026-11-08: detector built and tested, 22 tests OK; fixture-proven strongest-alert + two watch bands + value-trap rejection + "why not" explanations + briefing lines + JSON-lines run log; on real data 0 candidates and 0 traps — only MSFT scannable, overvalued — see PROJECT_STATUS.md Step 26 section)
27. Build financial risk and deterioration detection — PARTIAL (2026-11-08: four-area deterioration engine built and tested, 24 tests OK; MSFT FY2026 overall warning, no material deterioration; Step 23 flag now driven by Step 27, MSFT tier unchanged; MSFT only — see PROJECT_STATUS.md Step 27 section)
28. Build portfolio construction and position sizing — PARTIAL (2026-11-08: equal-weight / Kelly / fixed-fraction sizing, $1,000 small-cap basket by amount-at-risk, concentration checks and rebalancing proposals built and tested, 44 tests OK; fixture-proven only — no real candidate ever detected, so nothing sized on real data; no trade ever placed, proposals only — see PROJECT_STATUS.md Step 28 section. Note: the roadmap previously listed Step 28 as "company comparison and competitor analysis"; this step was built as portfolio construction per the Step 28 brief, and company comparison was built as Step 28b)
28b. Build company comparison and competitor analysis — PARTIAL (2026-11-08: side-by-side peer comparison orchestrating Steps 17–24 and 27 built and tested, 13 tests OK; MSFT profile verified on real data but no real peer comparison has ever run — NVDA/AVGO/GOOGL have no statements — so no ranking is meaningful on real data; fixture-proven three-company comparison with correct ranks and tie handling — see PROJECT_STATUS.md Step 28b section)
29. Build automated company research reports — PARTIAL (2026-11-08: report generator composing Steps 16–28b into dict + Markdown built and tested, 13 tests OK; MSFT report generated — tier overvalued, quality excellent, range $98.52/$163.30/$234.64, alert price $78.82; DBS degrades to insufficient-data; no business description exists in the DB, so none is invented; no "buy"/"sell" language in generated reports — see PROJECT_STATUS.md Step 29 section)
30. Build investment thesis tracking — PARTIAL (2026-11-08: thesis records with evidence snapshots, confirm/invalidate conditions with machine-checkable rules, review with suggested status, JSON-file storage under data/theses/, built and tested, 26 tests OK; example MSFT thesis created — tier overvalued, quality excellent, alert price $78.82, review on unchanged data stays active; no real thesis tracked over time yet — see PROJECT_STATUS.md Step 30 section)

### Part 6 — Market intelligence and portfolio

31. Build market news collection — PARTIAL (2026-11-08: collector built and tested, 29 tests OK; live MSFT collection ran — 5 real dated articles via the ddgs news endpoint, plus 5 hub pages from the earlier text-search probe; storage is JSON run files under data/news/, no DB writes — see PROJECT_STATUS.md Step 31 section)
32. Build daily market summaries — PARTIAL (2026-11-08: summary generator built and tested, 10 tests OK; real 2026-11-08 summary generated — 10,619 tickers scanned, 1 scannable (MSFT, overvalued), 0 candidates, 0 traps, no same-day news; movers never run on real data — only one stored price point per ticker; no sending/scheduling — see PROJECT_STATUS.md Step 32 section)
32. Build daily market summaries — NOT STARTED
33. Build earnings calendar and event tracking — PARTIAL (2026-11-09: event store built and tested, 22 tests OK; caller-supplied events only in JSON files under data/events/, no live feed; 12 week-ahead events seeded from the owner's research — KSPI Mon 9 Nov, ALC + US CPI Tue 10 Nov, SIA/SATS + bond market closed Wed 11 Nov, CSCO/AMAT Thu 12 Nov, Singtel + PPI Fri 13 Nov, 13Fs Mon 16 Nov, NVDA Tue 17 Nov; post-event re-check re-runs the Step 23 summary and compares tier/quality/health/deterioration/range — MSFT unchanged, fixture tier change detected, NVDA insufficient_data; no outcome ever recorded on real data — see PROJECT_STATUS.md Step 33 section. Note: the roadmap previously listed Step 33 as "macroeconomic and sector analysis"; this step was built as earnings calendar and event tracking per the Step 33 brief, and macro/sector analysis was built as Step 33b)
33b. Build macroeconomic and sector analysis — PARTIAL (2026-11-09: macro record store (one JSON file per point under data/macro/) + sector aggregation and sector-vs-market comparison built and tested, 20 tests OK; macro store seeded with 6 caller-supplied points for 5–6 Nov 2026 (S&P −4.36%, Nasdaq −5.52%, SOX −12.25%, VIX 28.18, week −2.7%/−3.4%); MSFT-only sector aggregates on real data — every aggregate a single-company figure, selection by stored sector empty (sector NULL for every US/HK row); sector-vs-market on real data is MSFT vs MSFT (difference 0.0); fixture-proven three-company medians and comparison — see PROJECT_STATUS.md Step 33b section)
34. Build company news and event monitoring — PARTIAL (2026-11-09: watchlist monitor over stored Step 31 news and Step 33 events built and tested, 32 tests OK; new-item detection with seen-set state, what-changed via Step 30 snapshots, alert records raised once per key, briefing lines; first real run on MSFT — 10 new articles, 0 MSFT events, 3 market-wide events, 0 alerts; no alert has ever fired on real data — see PROJECT_STATUS.md Step 34 section)
35. Build watchlist and financial tracker foundation — PARTIAL (2026-11-09: JSON watchlist store with reasons/sources/notes, per-ticker tracker records refreshed by the Step 25 scanner, and a one-line-per-ticker watchlist view built and tested, 24 tests OK; seeded with the owner's 38-ticker list, GOOGL carrying the Ackman Q2 2026 exit as a note; MSFT only scannable on real data — tier overvalued, quality excellent, alert price $78.82; legacy DB watchlist (3 rows) untouched and deliberately not synced — see PROJECT_STATUS.md Step 35 section. Note: the roadmap previously listed Step 35 as "portfolio management — BASIC FOUNDATION EXISTS"; this step was built as watchlist and financial tracker foundation per the Step 35 brief)
36. Build portfolio performance and risk analysis — PARTIAL (2026-11-09: pure analysis over caller-supplied holdings and price histories built and tested, 40 tests OK; total/per-position return, realised vs unrealised gain, TWR, MWR/IRR, volatility, max drawdown, beta, concentration on current value with Step 28 thresholds, income/yields, risk flags; fixture-proven 3-position portfolio +33.3% total return; MSFT on real data has only one price point so every history-dependent metric is missing — simple return +26.78% on a $400 cost basis is computable; no real portfolio ever analysed — see PROJECT_STATUS.md Step 36 section)
37. Build quarterly portfolio reporting — PARTIAL (2026-11-09: dict + Markdown quarterly reports built and tested, 23 tests OK; quarter parsing, quarter return/TWR/MWR, benchmark and excess return, contributors/detractors, risk snapshot, income, per-holding valuation snapshots from caller-supplied data or the Step 25 scanner, events from Step 33/31 files, quarter-over-quarter comparison, limitations section, one JSON + Markdown file per quarter; fixture-proven Q3 2026 example — value $3,000 → $3,600, quarter return +20.00% price only / +21.00% including dividends, benchmark +10.00%; MSFT on real data has only one price point so every history-dependent metric is missing, valuation snapshot tier overvalued / quality excellent / alert price $78.82; no holdings store built — the owner has never entered his holdings; no real portfolio ever reported on — see PROJECT_STATUS.md Step 37 section)
38. Build automated watchlist and valuation alerts — PARTIAL (2026-11-09: alert engine built and tested, 34 tests OK; rules strongest_alert, price_below_alert_price, watch_discount/watch_below_base, deterioration_material, quality_changed, thesis_confirmed/invalidated; dedupe once per key, clearing with re-fire count, acknowledge, one JSON state file under data/alerts/, no production writes, no sending/scheduling; fixture-proven — strongest_alert:FIXA at $5.00, watch_discount:FIXB at $21.00, second run raises nothing new, FIXA at $100.00 clears; MSFT on real data produces no alerts — tier overvalued at $507.12, alert price $78.82; no alert has ever fired on real data — see PROJECT_STATUS.md Step 38 section)

### Part 7 — JARVIS general-purpose assistant

39. Build unified AI orchestration — PARTIAL (2026-11-09: request pipeline built and tested, 27 tests OK; keyword intent classification with a labelled model fallback, guardrails refusing trade and send/schedule requests, routing to Step 16-38 modules, sourced replies, JSON-lines turn log under data/orchestration/; `agent/brain.py` and `agent/planner.py` are empty stubs, nothing reused from them; MSFT valuation traces to Step 21/23 outputs — tier overvalued, range $98.52/$163.30/$234.64, alert price $78.82; stub-tested only — no live Ollama turn has ever run through it; not wired into `main.py` (Step 50) — see PROJECT_STATUS.md Step 39 section)
40. Integrate cloud AI models — NOT STARTED
41. Integrate financial research agents — NOT STARTED
42. Build general-purpose web research — NOT STARTED
43. Build long-term contextual memory — NOT STARTED
44. Build natural-language tool calling — NOT STARTED
45. Build voice interaction — NOT STARTED
46. Build reminders and scheduled automation — NOT STARTED
47. Build computer and application interaction — NOT STARTED

### Part 8 — Final application and deployment

48. Build complete user interface — NOT STARTED
49. Build live dashboards and agent monitoring — NOT STARTED
50. Integrate all modules into main.py — NOT STARTED
51. Complete end-to-end testing — NOT STARTED
52. Build backup, recovery and security systems — NOT STARTED
53. Deploy reliable always-on assistant — NOT STARTED

## 11. Current Development Position

CURRENT STEP: 15F — US SEC ISSUER ENRICHMENT

Target file:

`agents/us_issuer_enrichment.py`

Purpose:

- Read existing US ticker and CIK records.
- Retrieve SEC submissions metadata using CIK.
- Collect issuer names, SIC codes, industry descriptions and other metadata.
- Link issuer information to existing ticker records.
- Preserve multiple securities belonging to one issuer.
- Produce enrichment CSV and report.
- Do not modify the existing SQLite database unless explicitly approved.

The file was included in the initial Git commit, but successful execution and output validation have not yet been confirmed.

Next step after successful validation:

15G — US security classification.

## 12. How to Work With the User

The user prefers:

- Step-by-step development.
- Complete file implementations rather than piecemeal code patches.
- Exact file paths.
- Clear explanations of why a change is needed.
- Minimal repetitive testing.
- Preservation of working functionality.
- A chronological roadmap from Step 1 through Step 53.

Do not overwhelm the user with many unrelated tasks.

Work on the current roadmap step first.

Before moving to the next major step, summarize the result and obtain approval.

The roadmap is a development plan, not proof that all previously implemented features are fully validated.

Always distinguish between:
- Implemented
- Tested
- Validated
- Production-ready

Never claim production readiness without appropriate evidence.