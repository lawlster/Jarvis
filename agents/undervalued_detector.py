"""Step 26 — Undervalued-company detection (Phase D).

Built on the Step 25 market scanner, not a second scanner: it runs
``agents.market_scanner.scan_market`` (which itself runs the Step
16-24 pipeline and the Step 23 tier logic) and filters the ranked
results into three buckets:

* **Candidates** — tickers worth a closer look, in two bands:
  - ``strongest_alert`` — the Step 23 tier: price at least 20%
    below the conservative intrinsic value, no material
    deterioration, business quality adequate or better.
  - ``watch`` — close to an alert or below base value, with the
    same quality and deterioration gates: either the price is
    below the conservative value but by less than 20%
    (``watch_discount``), or the price is below the base value
    but above the conservative value (``watch_below_base``).
    A watch name is worth keeping in mind; it is not an alert.
* **Value traps** — a price below the conservative value on a
  business that is weak or deteriorating.  Reported with the
  evidence (quality rating, health verdict, deterioration
  signals), never as a candidate.
* **Everything else** — overvalued, fairly valued, or
  insufficient data, each with a "why not" explanation
  (``explain_ticker``) instead of silence.

Detection bands (judgement rules, documented here and in code):

* The alert threshold is Step 23's ``STRONGEST_ALERT_MARGIN``
  (20% below conservative value).  Exactly 20% qualifies.
* A watch band requires the same gates as an alert: business
  quality adequate or better, deterioration False (not True and
  not unknown — an unknown deterioration flag excludes a name
  exactly as it blocks an alert in Step 23), and the strongest
  alert not blocked.  A deteriorating or weak business at a
  reasonable price is not a watch candidate.
* A value trap requires a price at or below the conservative
  value (margin vs conservative >= 0) AND weak/insufficient
  quality or material deterioration.  A weak business whose
  price is above its conservative value is not a trap — it is
  just a weak business at a fair price, and ``explain_ticker``
  says so.

Every detection record carries the valuation range, both margin
conventions (labelled — Step 23 Graham vs Step 21/22
(value - price) / price), the quality rating, the health verdict,
the deterioration flag, the price with its as-of date and source,
the reasons it qualifies, and the reasons it does NOT qualify for
anything stronger.  Nothing here is a buy/sell call: candidates
are "worth a closer look" and the final decision is always the
owner's.

Alert-ready output: ``briefing_line`` gives one line per
candidate (ticker, band, margin, quality, one-line reason) for a
daily check or morning briefing; ``briefing_summary`` gives the
counts plus those lines.  This module produces the record only —
it never sends anything and never schedules anything; delivery
is a later step.

Run history: ``append_run_log`` writes one JSON line per run
(date, universe size, scannable count, candidates by band, traps
found, tier counts) to ``data/detection_runs.jsonl`` in the
project (or a caller-supplied path).  It is a file log, not a
production-database table — no migration, no production writes.
This module makes no network requests.
"""

import json
import os
import sqlite3
import time
from collections import Counter

from agents import market_scanner as ms
from agents import margin_of_safety as mos

# Bands a candidate can fall into.
BAND_STRONGEST_ALERT = "strongest_alert"
BAND_WATCH_DISCOUNT = "watch_discount"
BAND_WATCH_BELOW_BASE = "watch_below_base"

# Quality ratings that pass the candidate gate (Step 24).
ADEQUATE_QUALITY = ("excellent", "good", "adequate")

# Default watch band: price below the conservative value by less
# than the alert threshold (default 0% to 20% discount).
DEFAULT_WATCH_DISCOUNT_MIN = 0.0

# Default run-log location, relative to the project root.
DEFAULT_RUN_LOG = os.path.join("data", "detection_runs.jsonl")


# ---------------------------------------------------------------------------
# Helpers over scanner candidates
# ---------------------------------------------------------------------------

def _deterioration(candidate):
    """Deterioration dict from the candidate's Step 23 detail, or None."""
    detail = candidate.get("detail") or {}
    deterioration = detail.get("deterioration")
    if isinstance(deterioration, dict):
        return deterioration
    return None


def _margin_vs_price(candidate, point="conservative"):
    """Step 21/22 margin convention for a range point, or None."""
    detail = candidate.get("detail") or {}
    margins = detail.get("margins_of_safety") or {}
    result = margins.get(point) or {}
    return result.get("margin_vs_price")


def _quality_passes(candidate):
    """True when the quality rating is adequate or better."""
    return candidate.get("quality_rating") in ADEQUATE_QUALITY


def _deterioration_clear(candidate):
    """True only when deterioration is known False.

    Unknown (None) does not clear a name — exactly as Step 23
    blocks a strongest alert on an unknown deterioration flag.
    """
    deterioration = _deterioration(candidate)
    if deterioration is None:
        return False
    return deterioration.get("deteriorated") is False


def _is_trap(candidate):
    """Value-trap test: cheap on price, weak or deteriorating.

    Requires a price at or below the conservative value AND
    (weak/insufficient quality OR material deterioration).
    Returns (is_trap, trap_reasons).
    """
    margin = candidate.get("margin_of_safety_vs_conservative")
    if margin is None or margin < 0:
        return False, []
    reasons = []
    quality = candidate.get("quality_rating")
    if quality not in ADEQUATE_QUALITY:
        reasons.append(
            f"business quality rating is '{quality}' (adequate or "
            "better required for a candidate)")
    deterioration = _deterioration(candidate)
    if deterioration is not None and deterioration.get("deteriorated"):
        signals = deterioration.get("signals", [])
        reasons.append(
            "material deterioration: " + "; ".join(signals)
            if signals else "material deterioration (Step 18/19/20 rule)")
    if candidate.get("health_verdict") == "weak":
        reasons.append(
            "Step 18 health verdict is 'weak' (a hard distress flag "
            "fired)")
    return bool(reasons), reasons


def _watch_band(candidate):
    """Watch sub-band for a scannable, ungated candidate, or None.

    ``watch_discount``: price below conservative value by less
    than the alert threshold.  ``watch_below_base``: price below
    base value but above conservative value.  Returns None when
    the price is above base value (fairly valued or overvalued)
    or when a margin is missing.
    """
    margin_cons = candidate.get("margin_of_safety_vs_conservative")
    margin_base = candidate.get("margin_of_safety_vs_base")
    if margin_cons is None or margin_base is None:
        return None
    if 0.0 <= margin_cons < mos.STRONGEST_ALERT_MARGIN:
        return BAND_WATCH_DISCOUNT
    if margin_cons < 0.0 and margin_base >= 0:
        return BAND_WATCH_BELOW_BASE
    return None


# ---------------------------------------------------------------------------
# Detection records
# ---------------------------------------------------------------------------

def _detection_record(candidate, band):
    """Full detection record for a candidate, from a scanner result."""
    detail = candidate.get("detail") or {}
    deterioration = _deterioration(candidate)
    tier_reasons = list(candidate.get("tier_reasons", []))
    if band == BAND_STRONGEST_ALERT:
        qualifies = tier_reasons + [
            "strongest-alert band: margin vs conservative value "
            ">= 20% with no material deterioration and quality "
            "adequate or better"]
        not_stronger = []
    else:
        margin_cons = candidate.get("margin_of_safety_vs_conservative")
        qualifies = tier_reasons + [
            f"watch band '{band}': margin vs conservative value "
            f"{margin_cons:.1%} is below the 20% strongest-alert "
            "threshold" if margin_cons is not None else
            f"watch band '{band}': price is below base value with "
            "quality adequate or better and no deterioration"]
        not_stronger = [
            "does not qualify for strongest_alert: margin vs "
            "conservative value is below 20%",
            "would qualify if the price fell to 80% of conservative "
            "value with no material deterioration (threshold price "
            f"{candidate.get('strongest_alert_threshold_price')})",
        ]
    return {
        "ticker": candidate["ticker"],
        "company_name": candidate.get("company_name"),
        "country": candidate.get("country"),
        "exchange": candidate.get("exchange"),
        "sector": candidate.get("sector"),
        "security_type": candidate.get("security_type"),
        "detection_band": band,
        "tier": candidate["tier"],
        "price": candidate.get("price"),
        "price_as_of": candidate.get("price_as_of"),
        "price_source": candidate.get("price_source"),
        "fair_value_range": candidate.get("fair_value_range"),
        "margin_of_safety_vs_conservative": candidate.get(
            "margin_of_safety_vs_conservative"),
        "margin_vs_price_vs_conservative": _margin_vs_price(
            candidate, "conservative"),
        "margin_of_safety_vs_base": candidate.get(
            "margin_of_safety_vs_base"),
        "margin_vs_price_vs_base": _margin_vs_price(candidate, "base"),
        "margin_of_safety_vs_optimistic": candidate.get(
            "margin_of_safety_vs_optimistic"),
        "margin_vs_price_vs_optimistic": _margin_vs_price(
            candidate, "optimistic"),
        "margin_convention": (
            "margin_of_safety is Step 23 (Graham): "
            "(value - price) / value. margin_vs_price is Step 21/22: "
            "(value - price) / price."),
        "strongest_alert_threshold_price": candidate.get(
            "strongest_alert_threshold_price"),
        "quality_rating": candidate.get("quality_rating"),
        "quality_mean_score": candidate.get("quality_mean_score"),
        "health_verdict": candidate.get("health_verdict"),
        "health_reasons": list(candidate.get("health_reasons", [])),
        "deteriorated": (deterioration or {}).get("deteriorated"),
        "deterioration_unknown": (deterioration or {}).get("unknown"),
        "deterioration_signals": list(
            (deterioration or {}).get("signals", [])),
        "deterioration_cautions": list(
            (deterioration or {}).get("cautions", [])),
        "qualifies_reasons": qualifies,
        "not_stronger_reasons": not_stronger,
        "conditions_to_change": list(
            candidate.get("conditions_to_change", [])),
        "starting_fcf_basis": candidate.get("starting_fcf_basis"),
        "scanner_rank": candidate.get("rank"),
        "rank": None,
        "warnings": list(candidate.get("warnings", [])),
        "detail": detail,
    }


def _trap_record(candidate, trap_reasons):
    """Detection-style record for a value trap (never a candidate)."""
    deterioration = _deterioration(candidate)
    return {
        "ticker": candidate["ticker"],
        "company_name": candidate.get("company_name"),
        "country": candidate.get("country"),
        "exchange": candidate.get("exchange"),
        "sector": candidate.get("sector"),
        "security_type": candidate.get("security_type"),
        "detection_band": "value_trap",
        "tier": candidate["tier"],
        "price": candidate.get("price"),
        "price_as_of": candidate.get("price_as_of"),
        "price_source": candidate.get("price_source"),
        "fair_value_range": candidate.get("fair_value_range"),
        "margin_of_safety_vs_conservative": candidate.get(
            "margin_of_safety_vs_conservative"),
        "margin_vs_price_vs_conservative": _margin_vs_price(
            candidate, "conservative"),
        "strongest_alert_threshold_price": candidate.get(
            "strongest_alert_threshold_price"),
        "quality_rating": candidate.get("quality_rating"),
        "quality_mean_score": candidate.get("quality_mean_score"),
        "health_verdict": candidate.get("health_verdict"),
        "health_reasons": list(candidate.get("health_reasons", [])),
        "deteriorated": (deterioration or {}).get("deteriorated"),
        "deterioration_signals": list(
            (deterioration or {}).get("signals", [])),
        "trap_reasons": list(trap_reasons),
        "why_not_a_candidate": (
            "cheap on price but weak or deteriorating: a low price "
            "alone is not value — " + "; ".join(trap_reasons)),
        "conditions_to_change": list(
            candidate.get("conditions_to_change", [])),
        "scanner_rank": candidate.get("rank"),
        "warnings": list(candidate.get("warnings", [])),
        "detail": candidate.get("detail"),
    }


# ---------------------------------------------------------------------------
# "Why not" explanations
# ---------------------------------------------------------------------------

def explain_ticker(candidate):
    """Why a ticker did or did not qualify as a candidate.

    Takes one Step 25 scanner candidate dict.  Returns
    {"ticker", "qualifies", "category", "reasons",
    "conditions_to_change"}.  Categories: ``strongest_alert``,
    ``watch``, ``value_trap``, ``blocked_alert`` (the margin
    qualifies but the alert is blocked and it is not a trap —
    e.g. deterioration unknown), ``overvalued``, ``fairly_valued``,
    ``reasonable_price`` (not a watch candidate — weak quality or
    deterioration), ``insufficient_data``.
    """
    ticker = candidate["ticker"]
    tier = candidate.get("tier")
    conditions = list(candidate.get("conditions_to_change", []))

    if tier == "insufficient_data":
        reasons = list(candidate.get("missing_inputs", [])) or \
            list(candidate.get("tier_reasons", []))
        return {
            "ticker": ticker, "qualifies": False,
            "category": "insufficient_data",
            "reasons": reasons or [
                "no valuation could be computed from the available "
                "data"],
            "conditions_to_change": conditions,
        }

    if tier == "strongest_alert":
        return {
            "ticker": ticker, "qualifies": True,
            "category": "strongest_alert",
            "reasons": list(candidate.get("tier_reasons", [])),
            "conditions_to_change": conditions,
        }

    is_trap, trap_reasons = _is_trap(candidate)
    if is_trap:
        return {
            "ticker": ticker, "qualifies": False,
            "category": "value_trap",
            "reasons": trap_reasons,
            "conditions_to_change": conditions,
        }

    if candidate.get("tier") == "reasonable_price":
        band = _watch_band(candidate)
        blocked = any("BLOCKED" in r
                      for r in candidate.get("tier_reasons", []))
        if band is not None and _quality_passes(candidate) \
                and _deterioration_clear(candidate) and not blocked:
            return {
                "ticker": ticker, "qualifies": True,
                "category": "watch",
                "reasons": list(candidate.get("tier_reasons", [])) + [
                    f"watch band '{band}': below the 20% "
                    "strongest-alert threshold"],
                "conditions_to_change": conditions,
            }
        if blocked:
            return {
                "ticker": ticker, "qualifies": False,
                "category": "blocked_alert",
                "reasons": list(candidate.get("tier_reasons", [])),
                "conditions_to_change": conditions,
            }
        reasons = list(candidate.get("tier_reasons", []))
        if not _quality_passes(candidate):
            reasons.append(
                f"business quality rating is "
                f"'{candidate.get('quality_rating')}' — a watch "
                "candidate needs adequate or better")
        if not _deterioration_clear(candidate):
            reasons.append(
                "deterioration is not known to be clear "
                "(deteriorated or unknown)")
        return {
            "ticker": ticker, "qualifies": False,
            "category": "reasonable_price",
            "reasons": reasons,
            "conditions_to_change": conditions,
        }

    if tier == "fairly_valued":
        margin_base = candidate.get("margin_of_safety_vs_base")
        margin_opt = candidate.get("margin_of_safety_vs_optimistic")
        return {
            "ticker": ticker, "qualifies": False,
            "category": "fairly_valued",
            "reasons": list(candidate.get("tier_reasons", [])) + [
                f"price is inside the fair-value range (margin vs "
                f"base {margin_base:.1%}, vs optimistic "
                f"{margin_opt:.1%})" if margin_base is not None
                and margin_opt is not None else
                "price is inside the fair-value range"],
            "conditions_to_change": conditions,
        }

    if tier == "overvalued":
        margin_opt = candidate.get("margin_of_safety_vs_optimistic")
        return {
            "ticker": ticker, "qualifies": False,
            "category": "overvalued",
            "reasons": list(candidate.get("tier_reasons", [])) + [
                f"price is above the optimistic value (margin vs "
                f"optimistic {margin_opt:.1%})"
                if margin_opt is not None else
                "price is above the optimistic value"],
            "conditions_to_change": conditions,
        }

    return {
        "ticker": ticker, "qualifies": False,
        "category": tier or "unknown",
        "reasons": list(candidate.get("tier_reasons", [])),
        "conditions_to_change": conditions,
    }


# ---------------------------------------------------------------------------
# Detection over a scan result
# ---------------------------------------------------------------------------

def detect_from_scan(scan):
    """Filter a Step 25 scan result into candidates and traps.

    ``scan`` is the dict returned by ``market_scanner.scan_market``
    (or ``scan_with_snapshot_prices``).  Returns {"candidates",
    "value_traps", "summary", "coverage", "run_seconds",
    "warnings"}.  Candidates are ranked strongest_alert first,
    then watch_discount, then watch_below_base; within a band
    the scanner's rank is kept, so the fresh-name rotation,
    margin and quality ordering from Step 25 survives (re-sorting
    by margin here would discard the rotation).
    """
    results = (scan or {}).get("results", [])
    candidates = []
    traps = []
    by_category = Counter()
    for candidate in results:
        explanation = explain_ticker(candidate)
        by_category[explanation["category"]] += 1
        if explanation["category"] == "strongest_alert":
            candidates.append(_detection_record(
                candidate, BAND_STRONGEST_ALERT))
        elif explanation["category"] == "watch":
            band = _watch_band(candidate)
            candidates.append(_detection_record(
                candidate, band or BAND_WATCH_BELOW_BASE))
        elif explanation["category"] == "value_trap":
            _, trap_reasons = _is_trap(candidate)
            traps.append(_trap_record(candidate, trap_reasons))

    band_order = {BAND_STRONGEST_ALERT: 0, BAND_WATCH_DISCOUNT: 1,
                  BAND_WATCH_BELOW_BASE: 2}

    def candidate_key(record):
        # Band first (strongest alert before watch), then the
        # scanner's rank within the band: the scanner already
        # orders by tier, fresh-name rotation, margin, quality and
        # ticker, and re-sorting by margin here would discard the
        # rotation (an earlier version did exactly that — FIXA
        # beat FIXB despite FIXA having been surfaced recently).
        return (band_order.get(record["detection_band"], 99),
                record.get("scanner_rank") if record.get(
                    "scanner_rank") is not None else float("inf"),
                str(record["ticker"]))

    candidates.sort(key=candidate_key)
    for index, record in enumerate(candidates, start=1):
        record["rank"] = index

    by_band = Counter(r["detection_band"] for r in candidates)
    coverage = (scan or {}).get("coverage", {})
    summary = {
        "selected": coverage.get("selected", len(results)),
        "scannable": coverage.get("scannable"),
        "candidates": len(candidates),
        "by_band": dict(by_band),
        "value_traps": len(traps),
        "by_category": dict(by_category),
        "strongest_alerts": by_band.get(BAND_STRONGEST_ALERT, 0),
        "watch": by_band.get(BAND_WATCH_DISCOUNT, 0)
                + by_band.get(BAND_WATCH_BELOW_BASE, 0),
    }
    return {
        "candidates": candidates,
        "value_traps": traps,
        "summary": summary,
        "coverage": coverage,
        "run_seconds": (scan or {}).get("run_seconds"),
        "warnings": list((scan or {}).get("warnings", [])),
    }


def detect_undervalued(conn, tickers=None, prices=None, country=None,
                       sector=None, security_type=None, exchange=None,
                       sector_overrides=None, already_surfed=None,
                       log_path=None):
    """Run the Step 25 scanner and detect undervalued companies.

    Same selection / price / override arguments as
    ``market_scanner.scan_market``.  ``already_surfed`` is passed
    through to the scanner's ranking (fresh-name rotation).
    ``log_path`` is a JSON-lines run-log path (or None for no log);
    see :func:`append_run_log`.  Returns the dict from
    :func:`detect_from_scan` plus the raw scan under ``"scan"``.
    No network requests, no database writes.
    """
    scan = ms.scan_market(
        conn, tickers=tickers, prices=prices, country=country,
        sector=sector, security_type=security_type, exchange=exchange,
        sector_overrides=sector_overrides,
        already_surfed=already_surfed)
    result = detect_from_scan(scan)
    result["scan"] = scan
    if log_path is not None:
        append_run_log(result, log_path)
    return result


def detect_with_snapshot_prices(conn, log_path=None, **kwargs):
    """Detect using the latest snapshot price for every ticker."""
    scan = ms.scan_with_snapshot_prices(conn, **kwargs)
    result = detect_from_scan(scan)
    result["scan"] = scan
    if log_path is not None:
        append_run_log(result, log_path)
    return result


# ---------------------------------------------------------------------------
# Alert-ready output (records only — never sent from here)
# ---------------------------------------------------------------------------

def briefing_line(record):
    """One line per candidate, for a daily check / morning briefing.

    Format: TICKER — band: margin% below conservative value
    ($value vs $price), quality X, health Y — one-line reason.
    Never a buy/sell call.
    """
    band = record["detection_band"]
    margin = record.get("margin_of_safety_vs_conservative")
    margin_text = f"{margin:.1%} below conservative value" \
        if margin is not None else "margin unknown"
    range_ = record.get("fair_value_range") or {}
    conservative = range_.get("conservative")
    value_text = (f"${conservative:,.2f}" if conservative is not None
                  else "value unknown")
    price = record.get("price")
    price_text = f"${price:,.2f}" if price is not None else "price unknown"
    reasons = record.get("qualifies_reasons") or []
    reason = reasons[-1] if reasons else ""
    band_label = {"strongest_alert": "STRONGEST ALERT",
                  "watch_discount": "watch (near alert)",
                  "watch_below_base": "watch (below base value)"}.get(
                      band, band)
    return (
        f"{record['ticker']} — {band_label}: {margin_text} "
        f"({value_text} vs {price_text}), quality "
        f"{record.get('quality_rating')}, health "
        f"{record.get('health_verdict')}"
        + (f" — {reason}" if reason else "")
        + " (worth a closer look; not a buy recommendation)")


def briefing_summary(detection):
    """Counts plus one briefing line per candidate.

    Returns {"headline", "lines", "trap_lines"}.  ``headline`` is
    a single summary line; ``lines`` are the candidate briefing
    lines; ``trap_lines`` are one line per value trap found.
    """
    summary = detection.get("summary", {})
    candidates = detection.get("candidates", [])
    traps = detection.get("value_traps", [])
    headline = (
        f"Undervalued detection: {summary.get('candidates', 0)} "
        f"candidate(s) ({summary.get('strongest_alerts', 0)} "
        f"strongest alert, {summary.get('watch', 0)} watch) and "
        f"{len(traps)} value trap(s) out of "
        f"{summary.get('scannable', '?')} scannable tickers "
        f"({summary.get('selected', '?')} selected)")
    trap_lines = []
    for trap in traps:
        margin = trap.get("margin_of_safety_vs_conservative")
        margin_text = f"{margin:.1%} below conservative value" \
            if margin is not None else "below conservative value"
        trap_lines.append(
            f"{trap['ticker']} — VALUE TRAP (not a candidate): "
            f"{margin_text}, quality {trap.get('quality_rating')}, "
            f"health {trap.get('health_verdict')} — "
            f"{trap.get('why_not_a_candidate', '')}")
    return {
        "headline": headline,
        "lines": [briefing_line(r) for r in candidates],
        "trap_lines": trap_lines,
    }


# ---------------------------------------------------------------------------
# Run history (JSON-lines file log, never the production database)
# ---------------------------------------------------------------------------

def append_run_log(detection, log_path):
    """Append one JSON line summarising a detection run.

    ``detection`` is the dict from :func:`detect_from_scan` (or
    :func:`detect_undervalued`).  Returns the log record written.
    The log directory is created when needed.  Raises on I/O
    errors — callers that must not fail on logging should catch.
    """
    summary = detection.get("summary", {})
    coverage = detection.get("coverage", {})
    record = {
        "run_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime()),
        "selected": summary.get("selected"),
        "scannable": summary.get("scannable"),
        "with_statements": coverage.get("with_statements"),
        "candidates": summary.get("candidates", 0),
        "by_band": summary.get("by_band", {}),
        "strongest_alerts": summary.get("strongest_alerts", 0),
        "watch": summary.get("watch", 0),
        "value_traps": summary.get("value_traps", 0),
        "by_category": summary.get("by_category", {}),
        "by_tier": coverage.get("by_tier", {}),
        "candidate_tickers": [r["ticker"] for r in
                              detection.get("candidates", [])],
        "trap_tickers": [r["ticker"] for r in
                         detection.get("value_traps", [])],
        "run_seconds": detection.get("run_seconds"),
    }
    directory = os.path.dirname(os.path.abspath(log_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def read_run_log(log_path):
    """Read a JSON-lines run log back into a list of records."""
    records = []
    if not os.path.exists(log_path):
        return records
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Dry-run entry point (no main.py changes)
# ---------------------------------------------------------------------------

def run_detection(db_path=None, log_path="default", **kwargs):
    """Dry-run entry: detect undervalued companies in a DB file.

    Opens the database read-only (``mode=ro``) and makes no writes
    to it and no network requests.  ``db_path`` defaults to
    ``database/assistant.db`` next to this package's project root.
    ``log_path`` defaults to ``data/detection_runs.jsonl`` in the
    project; pass None to skip logging.
    """
    if db_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "database",
                               "assistant.db")
    if log_path == "default":
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        log_path = os.path.join(project_root, DEFAULT_RUN_LOG)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return detect_with_snapshot_prices(conn, log_path=log_path,
                                           **kwargs)
    finally:
        conn.close()
