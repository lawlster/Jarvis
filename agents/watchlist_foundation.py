"""Step 35 — Watchlist and financial tracker foundation.

Consolidation layer over the watchlist and tracker tooling that
already exists in the project.  It builds only what was missing;
it does not replace or rewrite the legacy modules.

What already existed (audited 2026-11-09, see step35-report.md):

- ``agents/watchlist.py`` — a SQLite ``watchlist`` table with
  ``ticker`` (UNIQUE), ``company_name`` and ``added_at`` only.
  No reason, no source, no notes.  Three rows in the production
  DB (MSFT, GOOGL, NVDA, added 2026-11-03 by the renamed
  ``check_watchlist.py`` script).
- ``agents/financial_tracker.py`` — a SQLite
  ``financial_snapshots`` table; ``save_financial_snapshot``
  fetches live Yahoo data (network) and stores it.  Three rows,
  all MSFT.
- ``agents/valuation_monitor.py`` — a profile-gated DCF monitor.
  It returns "no valuation profile" for every ticker because
  ``valuation_profiles`` is empty, and it needs live Yahoo data
  plus yfinance shares outstanding.
- ``agents/valuation_profiles.py`` — CRUD for valuation
  profiles (DB table, 0 rows).
- ``agents/valuation_history.py`` — ``valuation_snapshots``
  table (created on demand; the table does not exist yet).
- ``agents/watchlist_research.py`` /
  ``agents/watchlist_valuation.py`` — loops over the legacy DB
  watchlist and run ``analyse_company`` /
  ``monitor_company_valuation`` on each ticker.
- ``agents/valuation_classifier.py``,
  ``agents/valuation_trends.py``, ``agents/alert_manager.py`` —
  classification, trend arithmetic and alert tables for the
  legacy valuation flow.
- ``agents/daily_summary.py`` and ``agents/news_monitoring.py``
  take a caller-supplied ticker list as their watchlist; neither
  owns a persistent watchlist store.

What was missing, and what this module adds:

1. A watchlist entry with a reason, one or more sources, notes
   and a date added — the legacy table cannot hold any of that.
2. A per-ticker tracker record: latest tier, quality, health,
   deterioration, valuation range, price and as-of date, and
   last-checked date, refreshed by running the Step 25 scanner
   over the watchlist.
3. A watchlist view: one line per ticker in the same shape as
   the Step 32 summary's watchlist notes, so the daily summary
   can consume stored tracker records without re-running a scan.
4. Dedupe: one entry per ticker no matter how many sources
   list it, with every source recorded; an investor exit (e.g.
   Ackman leaving GOOG/GOOGL in Q2 2026) is recorded as a note
   on the entry, never hidden.

Storage is JSON files under a caller-chosen directory (default
``data/watchlist/``), following the Step 26/30/31/33/34 file
pattern.  This module never writes to the production database
and creates no new DB tables, so the legacy modules keep
working exactly as before on their own three-ticker list.  The
legacy DB watchlist is deliberately NOT synced from here: the
two stores serve different purposes (legacy = old valuation
flow, this store = the Step 35 foundation) and merging them
would silently change production DB content.

A ticker is stored under the owner's spelling (e.g. ``BRK.B``,
``DBS``); where the market-universe ticker differs, the
``universe_ticker`` field carries it (``BRK-B``, ``D05``).
The tracker refresh scans by the universe ticker.

No network requests.  No buy/sell call anywhere; a tracker
record is a monitoring record, not a recommendation.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from agents.market_scanner import scan_ticker

DEFAULT_STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "watchlist"
WATCHLIST_FILENAME = "watchlist.json"
TRACKER_FILENAME = "tracker.json"
STORE_VERSION = 1

# Source labels.  Free-form strings are accepted; these are the
# labels the owner's seed list uses.
SOURCE_OWNER_CORE = "owner_core"
SOURCE_OWNER_SEMIS = "owner_semis"
SOURCE_OWNER_BASKET = "owner_basket_1000"
SOURCE_OWNER_SCREEN = "owner_combined_screen"
SOURCE_PABRAI_13F_Q2_2026 = "pabrai_13f_q2_2026"
SOURCE_ACKMAN_13F_Q2_2026 = "ackman_13f_q2_2026"
SOURCE_OWNER_SG_BANKS = "owner_sg_banks"
SOURCE_SCANNER_CANDIDATE = "scanner_candidate"

ACKMAN_GOOGL_EXIT_NOTE = (
    "Ackman exited GOOG/GOOGL in Q2 2026 (13F) — kept as a core "
    "watch, not an Ackman holding."
)

# The owner's watchlist, caller-supplied from his research
# (2026-11-03 to 2026-11-08), labelled as such.  Each entry is
# (ticker, universe_ticker or None, company_name, source,
# reason).  GOOGL appears once: it is a core holding, and the
# Ackman exit is recorded as a note by seed_watchlist.
OWNER_WATCHLIST_SEED = [
    # Core.
    ("GOOGL", None, "Alphabet Inc.", SOURCE_OWNER_CORE,
     "Owner core watchlist — good business at a good price screen."),
    ("UNH", None, "UnitedHealth Group Inc.", SOURCE_OWNER_CORE,
     "Owner core watchlist — good business at a good price screen."),
    ("BRK.B", "BRK-B", "Berkshire Hathaway Inc.", SOURCE_OWNER_CORE,
     "Owner core watchlist — good business at a good price screen."),
    # Semiconductors / energy.
    ("AVGO", None, "Broadcom Inc.", SOURCE_OWNER_SEMIS,
     "Semiconductor watch — AI cycle; value on normalised earnings, not peak earnings."),
    ("NVDA", None, "NVIDIA Corporation", SOURCE_OWNER_SEMIS,
     "Semiconductor watch — AI cycle; value on normalised earnings, not peak earnings."),
    ("AMD", None, "Advanced Micro Devices Inc.", SOURCE_OWNER_SEMIS,
     "Semiconductor watch — AI cycle; value on normalised earnings, not peak earnings."),
    ("INTC", None, "Intel Corporation", SOURCE_OWNER_SEMIS,
     "Semiconductor watch — possible value trap on a deteriorating business; monitor, do not assume cheap."),
    ("BE", None, "Bloom Energy Corporation", SOURCE_OWNER_SEMIS,
     "Energy watch — AI data-centre power demand."),
    # $1,000 small-cap asymmetric-bet basket.
    ("ALG", None, "Allegro MicroSystems Inc.", SOURCE_OWNER_BASKET,
     "$1,000 small-cap asymmetric-bet basket — accept-the-loss discipline."),
    ("VC", None, "Visteon Corporation", SOURCE_OWNER_BASKET,
     "$1,000 small-cap asymmetric-bet basket — accept-the-loss discipline."),
    ("HIMX", None, "Himax Technologies Inc.", SOURCE_OWNER_BASKET,
     "$1,000 small-cap asymmetric-bet basket — accept-the-loss discipline."),
    ("NRP", None, "Natural Resource Partners L.P.", SOURCE_OWNER_BASKET,
     "$1,000 small-cap asymmetric-bet basket — accept-the-loss discipline."),
    ("MCRI", None, "Monarch Casino & Resort Inc.", SOURCE_OWNER_BASKET,
     "$1,000 small-cap asymmetric-bet basket — accept-the-loss discipline."),
    # Combined Buffett/Munger + Graham + Greenblatt screen.
    ("BKE", None, "The Buckle Inc.", SOURCE_OWNER_SCREEN,
     "Combined Buffett/Munger + Graham + Greenblatt screen."),
    ("DECK", None, "Deckers Outdoor Corporation", SOURCE_OWNER_SCREEN,
     "Combined Buffett/Munger + Graham + Greenblatt screen."),
    ("DPZ", None, "Domino's Pizza Inc.", SOURCE_OWNER_SCREEN,
     "Combined Buffett/Munger + Graham + Greenblatt screen."),
    ("OTIS", None, "Otis Worldwide Corporation", SOURCE_OWNER_SCREEN,
     "Combined Buffett/Munger + Graham + Greenblatt screen."),
    ("DHI", None, "D.R. Horton Inc.", SOURCE_OWNER_SCREEN,
     "Combined Buffett/Munger + Graham + Greenblatt screen."),
    # Pabrai watch (Q2 2026 13F — research lead only, filing is lagged).
    ("HCC", None, "Warrior Met Coal Inc.", SOURCE_PABRAI_13F_Q2_2026,
     "Pabrai 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("RIG", None, "Transocean Ltd.", SOURCE_PABRAI_13F_Q2_2026,
     "Pabrai 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("AMR", None, "Alpha Metallurgical Resources Inc.", SOURCE_PABRAI_13F_Q2_2026,
     "Pabrai 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("KSPI", None, "Kaspi.kz", SOURCE_PABRAI_13F_Q2_2026,
     "Pabrai 13F Q2 2026 holding — research lead only, filing is lagged."),
    # Ackman watch (Q2 2026 13F — research lead only, filing is lagged).
    ("UBER", None, "Uber Technologies Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("BN", None, "Brookfield Corporation", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("MSFT", None, "Microsoft Corporation", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("AMZN", None, "Amazon.com Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("HHH", None, "Howard Hughes Holdings Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("QSR", None, "Restaurant Brands International Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("META", None, "Meta Platforms Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("V", None, "Visa Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("MA", None, "Mastercard Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("SPGI", None, "S&P Global Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("NFLX", None, "Netflix Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("ICE", None, "Intercontinental Exchange Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    ("ALC", None, "Alcon Inc.", SOURCE_ACKMAN_13F_Q2_2026,
     "Ackman 13F Q2 2026 holding — research lead only, filing is lagged."),
    # Singapore banks.
    ("DBS", "D05", "DBS Group Holdings Ltd", SOURCE_OWNER_SG_BANKS,
     "Singapore bank — Q3 2026 results monitored."),
    ("OCBC", "O39", "Oversea-Chinese Banking Corporation Ltd", SOURCE_OWNER_SG_BANKS,
     "Singapore bank — Q3 2026 results monitored."),
    ("UOB", "U11", "United Overseas Bank Ltd", SOURCE_OWNER_SG_BANKS,
     "Singapore bank — Q3 2026 results monitored."),
]


def _store_dir(store_dir=None):
    return Path(store_dir) if store_dir is not None else DEFAULT_STORE_DIR


def _watchlist_path(store_dir=None):
    return _store_dir(store_dir) / WATCHLIST_FILENAME


def _tracker_path(store_dir=None):
    return _store_dir(store_dir) / TRACKER_FILENAME


def _today_iso():
    return date.today().isoformat()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_ticker(ticker):
    if ticker is None:
        raise ValueError("ticker cannot be empty")
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("ticker cannot be empty")
    return ticker


def _new_entry(ticker, source, reason=None, company_name=None,
               universe_ticker=None, date_added=None):
    return {
        "ticker": _normalise_ticker(ticker),
        "universe_ticker": (
            _normalise_ticker(universe_ticker)
            if universe_ticker else None),
        "company_name": company_name,
        "sources": [source] if source else [],
        "reason": reason,
        "notes": [],
        "date_added": date_added or _today_iso(),
    }


def load_watchlist(store_dir=None):
    """All watchlist entries, in ticker order.  Empty list if none."""
    path = _watchlist_path(store_dir)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data.get("entries", [])
    return sorted(entries, key=lambda e: e["ticker"])


def save_watchlist(entries, store_dir=None):
    """Write the whole watchlist (list of entry dicts)."""
    directory = _store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "entries": sorted(entries, key=lambda e: e["ticker"]),
    }
    with open(_watchlist_path(store_dir), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return payload["entries"]


def get_entry(ticker, store_dir=None):
    """One entry by ticker, or None."""
    ticker = _normalise_ticker(ticker)
    for entry in load_watchlist(store_dir):
        if entry["ticker"] == ticker:
            return entry
    return None


def add_ticker(ticker, source, reason=None, company_name=None,
               universe_ticker=None, date_added=None, store_dir=None):
    """Add a ticker, or merge into the existing entry (dedupe).

    The same ticker from a second source is one entry with both
    sources recorded, never a duplicate row.  Returns
    ``(entry, action)`` where action is ``"added"`` or
    ``"updated"``.
    """
    ticker = _normalise_ticker(ticker)
    if source is not None and not str(source).strip():
        raise ValueError("source cannot be an empty string")
    entries = load_watchlist(store_dir)
    existing = next((e for e in entries if e["ticker"] == ticker), None)
    if existing is None:
        entry = _new_entry(ticker, source, reason=reason,
                           company_name=company_name,
                           universe_ticker=universe_ticker,
                           date_added=date_added)
        entries.append(entry)
        save_watchlist(entries, store_dir)
        return entry, "added"
    if source and source not in existing["sources"]:
        existing["sources"].append(source)
    if reason and not existing.get("reason"):
        existing["reason"] = reason
    if company_name and not existing.get("company_name"):
        existing["company_name"] = company_name
    if universe_ticker and not existing.get("universe_ticker"):
        existing["universe_ticker"] = _normalise_ticker(universe_ticker)
    save_watchlist(entries, store_dir)
    return existing, "updated"


def remove_ticker(ticker, store_dir=None):
    """Remove a ticker.  Returns True if it was there."""
    ticker = _normalise_ticker(ticker)
    entries = load_watchlist(store_dir)
    kept = [e for e in entries if e["ticker"] != ticker]
    if len(kept) == len(entries):
        return False
    save_watchlist(kept, store_dir)
    return True


def add_note(ticker, note, store_dir=None):
    """Append a note to an entry (no duplicates).  Returns the entry."""
    ticker = _normalise_ticker(ticker)
    entries = load_watchlist(store_dir)
    entry = next((e for e in entries if e["ticker"] == ticker), None)
    if entry is None:
        raise ValueError(f"{ticker} is not on the watchlist")
    if note and note not in entry["notes"]:
        entry["notes"].append(note)
    save_watchlist(entries, store_dir)
    return entry


def record_exit(ticker, source, note, store_dir=None):
    """Record that an investor/source exited a ticker.

    The source is removed from the entry's current sources and
    the note is appended, so the exit is visible on the entry
    rather than silently dropping the ticker.  Returns the entry.
    """
    ticker = _normalise_ticker(ticker)
    entries = load_watchlist(store_dir)
    entry = next((e for e in entries if e["ticker"] == ticker), None)
    if entry is None:
        raise ValueError(f"{ticker} is not on the watchlist")
    if source in entry["sources"]:
        entry["sources"].remove(source)
    if note and note not in entry["notes"]:
        entry["notes"].append(note)
    save_watchlist(entries, store_dir)
    return entry


def list_tickers(store_dir=None):
    """Owner-spelling tickers on the watchlist, sorted."""
    return [e["ticker"] for e in load_watchlist(store_dir)]


def seed_watchlist(store_dir=None, entries=None):
    """Seed the watchlist with the owner's list (default: his list).

    Idempotent: re-seeding merges sources into the existing
    entries instead of duplicating them.  Returns the entries.
    The Ackman exit from GOOG/GOOGL in Q2 2026 is recorded as a
    note on the GOOGL entry.
    """
    seed = list(entries) if entries is not None else OWNER_WATCHLIST_SEED
    for ticker, universe_ticker, company_name, source, reason in seed:
        add_ticker(ticker, source, reason=reason,
                   company_name=company_name,
                   universe_ticker=universe_ticker,
                   store_dir=store_dir)
    add_note("GOOGL", ACKMAN_GOOGL_EXIT_NOTE, store_dir=store_dir)
    return load_watchlist(store_dir)


def load_tracker(store_dir=None):
    """Tracker records by ticker.  Empty dict if none."""
    path = _tracker_path(store_dir)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return dict(data.get("records", {}))


def save_tracker(records, store_dir=None):
    """Write tracker records (dict keyed by ticker)."""
    directory = _store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "records": {k: records[k] for k in sorted(records)},
    }
    with open(_tracker_path(store_dir), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return payload["records"]


def get_tracker_record(ticker, store_dir=None):
    """One tracker record by ticker, or None."""
    return load_tracker(store_dir).get(_normalise_ticker(ticker))


def _tracker_record(entry, candidate, checked_at):
    """Flatten one Step 25 scanner result into a tracker record."""
    detail = candidate.get("detail") or {}
    deterioration_27 = detail.get("deterioration_27") or {}
    flags = deterioration_27.get("risk_flags") or []
    # Step 27 flag dicts carry the flag name under "name"
    # (older consumers used "flag"); accept either shape.
    flag_names = [f.get("name") or f.get("flag")
                  for f in flags if f.get("name") or f.get("flag")]
    return {
        "ticker": entry["ticker"],
        "universe_ticker": entry.get("universe_ticker"),
        "company_name": candidate.get("company_name")
        or entry.get("company_name"),
        "scannable": bool(candidate.get("scannable")),
        "tier": candidate.get("tier"),
        "tier_reasons": list(candidate.get("tier_reasons") or []),
        "quality_rating": candidate.get("quality_rating"),
        "quality_mean_score": candidate.get("quality_mean_score"),
        "health_verdict": candidate.get("health_verdict"),
        "health_reasons": list(candidate.get("health_reasons") or []),
        "fair_value_range": dict(candidate.get("fair_value_range") or {}),
        "margin_of_safety_vs_conservative": candidate.get(
            "margin_of_safety_vs_conservative"),
        "margin_of_safety_vs_base": candidate.get(
            "margin_of_safety_vs_base"),
        "price": candidate.get("price"),
        "price_as_of": candidate.get("price_as_of"),
        "price_source": candidate.get("price_source"),
        "strongest_alert_threshold_price": candidate.get(
            "strongest_alert_threshold_price"),
        "deterioration_overall_verdict": deterioration_27.get(
            "overall_verdict"),
        "deterioration_material": (
            (detail.get("deterioration") or {}).get("deteriorated")
            if isinstance(detail.get("deterioration"), dict) else None),
        "risk_flags": flag_names,
        "missing_inputs": list(candidate.get("missing_inputs") or []),
        "last_checked": checked_at,
    }


def refresh_watchlist(conn, watchlist=None, prices=None,
                      store_dir=None, checked_at=None):
    """Refresh the tracker for every watchlist entry.

    Runs the Step 25 scanner over the watchlist (one scan per
    ticker) and stores one tracker record per ticker.  ``watchlist``
    is a list of entry dicts (default: the stored watchlist).
    ``prices`` is an optional dict keyed by universe ticker (or
    owner ticker) with a caller-supplied price; without it the
    scanner falls back to the latest snapshot price, labelled.
    Returns ``{"records": [...], "summary": {...}}``.  Never writes
    to the database; tracker records are JSON only.
    """
    entries = (list(watchlist) if watchlist is not None
               else load_watchlist(store_dir))
    prices = dict(prices or {})
    checked_at = checked_at or _now_iso()
    records = {}
    cache = {}
    for entry in entries:
        scan_ticker_value = entry.get("universe_ticker") or entry["ticker"]
        price_input = prices.get(scan_ticker_value)
        if price_input is None:
            price_input = prices.get(entry["ticker"])
        candidate = scan_ticker(conn, scan_ticker_value,
                                price_input=price_input, cache=cache)
        records[entry["ticker"]] = _tracker_record(
            entry, candidate, checked_at)
    if store_dir is not None:
        save_tracker(records, store_dir)
    summary = {
        "tickers": len(entries),
        "scannable": sum(1 for r in records.values() if r["scannable"]),
        "insufficient_data": sum(
            1 for r in records.values() if not r["scannable"]),
        "tiers": {},
        "last_checked": checked_at,
    }
    for record in records.values():
        tier = record["tier"]
        summary["tiers"][tier] = summary["tiers"].get(tier, 0) + 1
    return {"records": [records[k] for k in sorted(records)],
            "summary": summary}


def view_line(record):
    """One watchlist line from a tracker record.

    Same shape as the Step 32 summary's watchlist notes: tier,
    quality, health, margin vs the conservative value, price with
    its as-of date, alert price, deterioration and risk flags, and
    last-checked date.  Never a recommendation.
    """
    ticker = record["ticker"]
    tier = record.get("tier")
    if not record.get("scannable"):
        reasons = list(record.get("missing_inputs") or [])
        return {
            "ticker": ticker,
            "note": (
                f"{ticker}: insufficient data — "
                + ("; ".join(reasons) if reasons else
                   "no valuation could be computed from the "
                   "available data")),
            "tier": tier,
            "quality_rating": record.get("quality_rating"),
            "health_verdict": record.get("health_verdict"),
            "price": record.get("price"),
            "price_as_of": record.get("price_as_of"),
            "last_checked": record.get("last_checked"),
        }
    margin = record.get("margin_of_safety_vs_conservative")
    margin_text = (f"{margin:.1%} vs conservative value"
                   if margin is not None else "margin unknown")
    price = record.get("price")
    price_text = (f"${price:,.2f}" if price is not None else "price unknown")
    if record.get("price_as_of"):
        price_text += f" (as of {record['price_as_of']})"
    alert = record.get("strongest_alert_threshold_price")
    alert_text = (f"alert price ${alert:,.2f}"
                  if alert is not None else "alert price unknown")
    det = record.get("deterioration_overall_verdict")
    det_text = f"deterioration {det}" if det else "deterioration unknown"
    flags = record.get("risk_flags") or []
    if flags:
        det_text += f" ({', '.join(flags)})"
    note = (
        f"{ticker}: {tier} | quality "
        f"{record.get('quality_rating')} | health "
        f"{record.get('health_verdict')} | {margin_text} | "
        f"price {price_text} | {alert_text} | {det_text} | "
        f"last checked {record.get('last_checked')}"
    )
    return {
        "ticker": ticker,
        "note": note,
        "tier": tier,
        "quality_rating": record.get("quality_rating"),
        "health_verdict": record.get("health_verdict"),
        "price": price,
        "price_as_of": record.get("price_as_of"),
        "last_checked": record.get("last_checked"),
    }


def watchlist_view(records=None, store_dir=None):
    """One-line view for every stored tracker record (or given ones)."""
    if records is None:
        tracker = load_tracker(store_dir)
        records = [tracker[k] for k in sorted(tracker)]
    return [view_line(record) for record in records]


def legacy_watchlist_count(db_path=None):
    """Row count of the legacy DB watchlist table (read-only check)."""
    base = Path(__file__).resolve().parent.parent
    path = Path(db_path) if db_path is not None else base / "database" / "assistant.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM watchlist").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
