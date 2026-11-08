"""Step 30 — Investment thesis tracking.

A thesis is the owner's reasoning about a company at a point in
time, recorded in the owner's own words and anchored to the
evidence the engine could see on that date (Steps 21-27 outputs:
valuation range, tier, quality, health, deterioration).  The
module records a thesis, snapshots the evidence at creation,
re-runs the pipeline on review and reports what changed and
whether any confirm / invalidate condition has been met.

Rules:
- The thesis text is caller-supplied.  It is never invented by
  the module: creating a thesis with blank text raises an error.
  The only exception is the labelled example thesis built by the
  Step 30 report, which is explicitly marked as an example, not
  the owner's words.
- Confirm / invalidate conditions are caller-supplied where
  given.  Where none are supplied, suggested defaults are built
  from the evidence snapshot (Step 23 alert price and tier,
  Step 24 quality, Step 27 deterioration) and labelled as such.
  A condition is a dict with a free-text ``description`` and an
  optional machine-checkable ``rule``; a condition with no rule
  is reported as "not machine-checkable — owner to judge", never
  silently treated as met or unmet.
- Storage is JSON files under a caller-chosen directory
  (default ``data/theses/`` under the project root), one file per
  thesis.  No new production database tables are created and
  nothing is written to the production database; the database is
  only read (through the Step 16-27 pipeline) to build snapshots.
- No recommendation is made anywhere.  A thesis is a record of
  reasoning, not a trade instruction; review output avoids the
  words "buy" and "sell" entirely.
- Statuses: ``active`` / ``confirmed`` / ``invalidated`` /
  ``stale`` (no fresh data could be computed on review) /
  ``closed`` (owner closed it).  Every status change is recorded
  in ``status_history`` with a date and a reason.
"""

import json
import os
from datetime import date
from pathlib import Path

from agents import financial_health as fh
from agents import financial_ratios as fr
from agents import margin_of_safety as mos
from agents import market_scanner as ms

THESIS_VERSION = "1.0"

STATUSES = ("active", "confirmed", "invalidated", "stale", "closed")

# Quality ratings ordered worst to best, for "quality falls below X"
# conditions.  "insufficient data" is the bottom: nothing is known.
QUALITY_RANK = {
    "insufficient data": 0,
    "insufficient_data": 0,
    "weak": 1,
    "adequate": 2,
    "good": 3,
    "excellent": 4,
}

DEFAULT_THESES_DIR = Path(__file__).resolve().parent.parent / "data" \
    / "theses"

MISSING_NO_STATEMENTS = (
    "no financial statement rows for this ticker in the database"
)

# Key figures carried in the evidence snapshot (latest annual).
SNAPSHOT_FIGURES = (
    ("revenue", "income_statement", "Revenue"),
    ("net_income", "income_statement", "Net income"),
    ("eps_diluted", "income_statement", "Diluted EPS"),
    ("operating_cash_flow", "cash_flow_statement",
     "Operating cash flow"),
    ("free_cash_flow", "cash_flow_statement", "Free cash flow"),
    ("total_equity", "balance_sheet", "Total equity"),
    ("total_debt", "balance_sheet", "Total debt"),
)


# ---------------------------------------------------------------------------
# Evidence snapshot
# ---------------------------------------------------------------------------


def _guarded(label, fn, warnings):
    """Run ``fn``; on failure return None and record the warning."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - snapshot, never crash
        warnings.append(f"{label} failed: {exc}")
        return None


def _latest_value(values, statement_type, concept):
    """Latest present annual value for a concept, or None."""
    periods = sorted(
        p for (s, p, t) in values
        if s == statement_type and t == "annual")
    for period in reversed(periods):
        row = values.get((statement_type, period, "annual")) or {}
        entry = row.get(concept) or {}
        if entry.get("present") and entry.get("value") is not None:
            return entry["value"], period
    return None, None


def build_evidence_snapshot(conn, ticker, price_input=None,
                            sector_override=None):
    """Snapshot the Step 21-27 evidence for ``ticker`` as of now.

    Returns a JSON-serialisable dict.  ``status`` is ``complete``
    when a valuation range and a price both exist, ``partial``
    when a range exists but no price does, and
    ``insufficient_data`` when no range could be computed.
    Nothing here is a recommendation.
    """
    ticker = ticker.upper()
    warnings = []
    price_input = ms.normalise_price_input(price_input)
    if price_input is None:
        price_input = _guarded(
            "snapshot price load",
            lambda: fr.load_snapshot_price(conn, ticker), warnings)

    # TTM basis first (the Step 21 default); if the ticker has no
    # quarterly rows the TTM starting FCF is missing and no range
    # can be computed, so fall back to the labelled annual basis —
    # the same fallback the Step 25 scanner and the Step 29
    # report use.
    summary = _guarded(
        "margin-of-safety summary",
        lambda: mos.margin_of_safety_summary(
            conn, ticker, price_input=price_input,
            sector_override=sector_override),
        warnings)
    fcf_basis = "ttm"
    range_values = (summary or {}).get("fair_value_range") or {}
    if summary is not None and all(
            range_values.get(point) is None
            for point in ("conservative", "base", "optimistic")):
        annual = _guarded(
            "margin-of-safety summary (annual FCF basis)",
            lambda: mos.margin_of_safety_summary(
                conn, ticker, price_input=price_input,
                sector_override=sector_override,
                starting_fcf_basis="annual"),
            warnings)
        if annual is not None:
            annual_range = annual.get("fair_value_range") or {}
            if any(annual_range.get(point) is not None
                   for point in ("conservative", "base", "optimistic")):
                summary = annual
                fcf_basis = ("annual (no quarterly rows, so no TTM "
                             "free cash flow exists)")
    health = _guarded(
        "health assessment",
        lambda: fh.assess_health(conn, ticker), warnings)

    figures = {}
    figure_periods = {}
    if summary is not None or health is not None:
        values, load_warnings = _guarded(
            "standardised values load",
            lambda: fr.load_standardised_values(conn, ticker),
            warnings) or ({}, [])
        warnings.extend(load_warnings)
        for concept, statement_type, _label in SNAPSHOT_FIGURES:
            value, period = _latest_value(values, statement_type,
                                          concept)
            figures[concept] = value
            figure_periods[concept] = period

    if summary is None:
        return {
            "ticker": ticker,
            "status": "insufficient_data",
            "reason": "valuation could not be computed",
            "reporting_period": None,
            "fcf_basis": fcf_basis,
            "price": None,
            "price_as_of": None,
            "price_source": None,
            "fair_value_range": {
                "conservative": None, "base": None,
                "optimistic": None},
            "margins_graham": {},
            "tier": None,
            "tier_reasons": [],
            "conditions_to_change": [],
            "strongest_alert_threshold_price": None,
            "entry_price_ladder": [],
            "quality_rating": None,
            "quality_mean_score": None,
            "health_verdict": (health or {}).get("verdict"),
            "health_reasons": (health or {}).get("verdict_reasons", []),
            "deterioration_verdict": None,
            "material_deterioration": None,
            "deterioration_unknown": True,
            "risk_flags": [],
            "key_figures": figures,
            "key_figure_periods": figure_periods,
            "warnings": warnings,
        }

    range_values = summary.get("fair_value_range") or {}
    tier = summary.get("tier") or {}
    quality = summary.get("business_quality") or {}
    deterioration_27 = summary.get("deterioration_27") or {}
    deterioration = summary.get("deterioration") or {}
    margins = summary.get("margins_of_safety") or {}
    ladder = summary.get("entry_price_ladder") or {}
    has_range = range_values.get("conservative") is not None
    has_price = summary.get("price") is not None
    status = ("complete" if has_range and has_price
              else "partial" if has_range else "insufficient_data")
    return {
        "ticker": ticker,
        "status": status,
        "reason": (None if status == "complete"
                   else "no conservative intrinsic value could be "
                   "computed" if status == "insufficient_data"
                   else "no price available"),
        "reporting_period": summary.get("reporting_period"),
        "fcf_basis": fcf_basis,
        "sector": summary.get("sector"),
        "sector_source": summary.get("sector_source"),
        "why_this_model": summary.get("why_this_model"),
        "price": summary.get("price"),
        "price_as_of": summary.get("price_as_of"),
        "price_source": summary.get("price_source"),
        "fair_value_range": {
            "conservative": range_values.get("conservative"),
            "base": range_values.get("base"),
            "optimistic": range_values.get("optimistic")},
        "margins_graham": {
            point: (entry or {}).get("value")
            for point, entry in margins.items()},
        "tier": tier.get("tier"),
        "tier_reasons": tier.get("reasons", []),
        "conditions_to_change": tier.get("conditions_to_change", []),
        "strongest_alert_blocked": tier.get("strongest_alert_blocked"),
        "strongest_alert_threshold_price": summary.get(
            "strongest_alert_threshold_price"),
        "entry_price_ladder": ladder.get("rungs", []),
        "quality_rating": quality.get("rating"),
        "quality_mean_score": quality.get("mean_score"),
        "health_verdict": (health or {}).get("verdict"),
        "health_reasons": (health or {}).get("verdict_reasons", []),
        "deterioration_verdict": deterioration_27.get(
            "overall_verdict"),
        "material_deterioration": deterioration_27.get(
            "material_deterioration")
        if deterioration_27.get("material_deterioration") is not None
        else deterioration.get("deteriorated"),
        "deterioration_unknown": bool(deterioration.get("unknown")),
        "risk_flags": [
            {"name": flag.get("name"),
             "severity": flag.get("severity"),
             "area": flag.get("area")}
            for flag in deterioration_27.get("risk_flags", [])],
        "key_figures": figures,
        "key_figure_periods": figure_periods,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

# Machine-checkable rule kinds.  Each rule is a dict:
#   {"kind": ..., "value": ...}
# ``description`` (free text) is always present on the condition;
# the rule is what the review can evaluate.  A condition without
# a rule is reported as not machine-checkable, never guessed at.
RULE_KINDS = (
    "price_at_or_below",
    "price_at_or_above",
    "tier_is",
    "tier_is_not",
    "material_deterioration",
    "quality_at_or_below",
    "quality_at_or_above",
    "margin_vs_conservative_at_least",
    "margin_vs_conservative_at_most",
    "conservative_value_at_or_below",
    "conservative_value_at_or_above",
)


def _quality_rank(rating):
    if rating is None:
        return None
    return QUALITY_RANK.get(str(rating).lower())


def evaluate_condition(condition, snapshot):
    """Evaluate one condition against a snapshot.

    Returns ``{"condition", "met", "reason"}`` where ``met`` is
    True / False, or None when the condition cannot be evaluated
    (no rule, unknown rule kind, or the snapshot lacks the input).
    """
    description = condition.get("description") or str(condition)
    rule = condition.get("rule") or {}
    kind = rule.get("kind")
    value = rule.get("value")
    if not kind:
        return {"condition": condition, "met": None,
                "reason": "no machine-checkable rule — owner to judge"}
    if kind not in RULE_KINDS:
        return {"condition": condition, "met": None,
                "reason": f"unknown rule kind '{kind}' — owner to judge"}

    price = snapshot.get("price")
    range_values = snapshot.get("fair_value_range") or {}
    margin = (snapshot.get("margins_graham") or {}).get("conservative")
    tier = snapshot.get("tier")
    quality = snapshot.get("quality_rating")
    deteriorated = snapshot.get("material_deterioration")

    def _missing(field):
        return {"condition": condition, "met": None,
                "reason": f"cannot evaluate: {field} missing from "
                "the snapshot"}

    if kind == "price_at_or_below":
        if price is None:
            return _missing("price")
        met = price <= value
        return {"condition": condition, "met": met,
                "reason": f"price {price} vs threshold {value}"}
    if kind == "price_at_or_above":
        if price is None:
            return _missing("price")
        met = price >= value
        return {"condition": condition, "met": met,
                "reason": f"price {price} vs threshold {value}"}
    if kind == "tier_is":
        if tier is None:
            return _missing("tier")
        met = tier == value
        return {"condition": condition, "met": met,
                "reason": f"tier is {tier}, condition requires {value}"}
    if kind == "tier_is_not":
        if tier is None:
            return _missing("tier")
        met = tier != value
        return {"condition": condition, "met": met,
                "reason": f"tier is {tier}, condition excludes {value}"}
    if kind == "material_deterioration":
        if deteriorated is None:
            return _missing("material deterioration flag")
        met = bool(deteriorated) is bool(value)
        return {"condition": condition, "met": met,
                "reason": f"material deterioration is {deteriorated}"}
    if kind == "quality_at_or_below":
        rank = _quality_rank(quality)
        target = _quality_rank(value)
        if rank is None or target is None:
            return _missing("quality rating")
        met = rank <= target
        return {"condition": condition, "met": met,
                "reason": f"quality rating is {quality}, condition "
                f"is at or below {value}"}
    if kind == "quality_at_or_above":
        rank = _quality_rank(quality)
        target = _quality_rank(value)
        if rank is None or target is None:
            return _missing("quality rating")
        met = rank >= target
        return {"condition": condition, "met": met,
                "reason": f"quality rating is {quality}, condition "
                f"is at or above {value}"}
    if kind == "margin_vs_conservative_at_least":
        if margin is None:
            return _missing("margin vs conservative value")
        met = margin >= value
        return {"condition": condition, "met": met,
                "reason": f"margin vs conservative value is "
                f"{margin:.4f}, condition requires at least {value}"}
    if kind == "margin_vs_conservative_at_most":
        if margin is None:
            return _missing("margin vs conservative value")
        met = margin <= value
        return {"condition": condition, "met": met,
                "reason": f"margin vs conservative value is "
                f"{margin:.4f}, condition requires at most {value}"}
    if kind == "conservative_value_at_or_below":
        conservative = range_values.get("conservative")
        if conservative is None:
            return _missing("conservative value")
        met = conservative <= value
        return {"condition": condition, "met": met,
                "reason": f"conservative value is {conservative} vs "
                f"threshold {value}"}
    if kind == "conservative_value_at_or_above":
        conservative = range_values.get("conservative")
        if conservative is None:
            return _missing("conservative value")
        met = conservative >= value
        return {"condition": condition, "met": met,
                "reason": f"conservative value is {conservative} vs "
                f"threshold {value}"}
    return {"condition": condition, "met": None,
            "reason": f"rule kind '{kind}' not implemented"}


def default_conditions(snapshot):
    """Suggested default confirm / invalidate conditions.

    Built from the evidence snapshot only.  They are suggestions,
    labelled as such — the caller may replace them entirely.
    A candidate condition that is already met in the creation
    snapshot is skipped (a thesis cannot be confirmed or
    invalidated by the state it was created in); skipped
    descriptions are returned third so the caller can see them.
    """
    candidates_confirm = []
    candidates_invalidate = []
    alert_price = snapshot.get("strongest_alert_threshold_price")
    if alert_price is not None:
        candidates_confirm.append({
            "description": (
                "Price falls to or below the strongest-alert "
                f"threshold price (${alert_price:,.2f})"),
            "rule": {"kind": "price_at_or_below",
                     "value": alert_price}})
    candidates_confirm.append({
        "description": "Tier becomes strongest_alert",
        "rule": {"kind": "tier_is", "value": "strongest_alert"}})
    candidates_confirm.append({
        "description": (
            "Margin of safety vs the conservative value reaches "
            "at least 20%"),
        "rule": {"kind": "margin_vs_conservative_at_least",
                 "value": 0.20}})
    candidates_invalidate.append({
        "description": "Material deterioration appears",
        "rule": {"kind": "material_deterioration", "value": True}})
    candidates_invalidate.append({
        "description": "Business quality falls to weak or below",
        "rule": {"kind": "quality_at_or_below", "value": "weak"}})
    candidates_invalidate.append({
        "description": "Tier becomes overvalued and stays there "
        "while the price rises — the entry case has gone away",
        "rule": {"kind": "tier_is", "value": "overvalued"}})

    confirm, invalidate, skipped = [], [], []
    for condition in candidates_confirm + candidates_invalidate:
        evaluated = evaluate_condition(condition, snapshot)
        if evaluated["met"] is True:
            skipped.append(condition.get("description"))
        elif condition in candidates_confirm:
            confirm.append(condition)
        else:
            invalidate.append(condition)
    return confirm, invalidate, skipped


# ---------------------------------------------------------------------------
# Thesis records
# ---------------------------------------------------------------------------


def _thesis_id(ticker, created_date, theses_dir):
    """Next sequence number for this ticker and date."""
    existing = sorted(Path(theses_dir).glob(
        f"{ticker}_{created_date}_*.json")) if theses_dir else []
    return f"{ticker}_{created_date}_{len(existing) + 1:02d}"


def create_thesis(conn, ticker, thesis_text,
                  confirm_conditions=None, invalidate_conditions=None,
                  price_input=None, sector_override=None,
                  created_date=None, author=None, theses_dir=None,
                  save=True):
    """Create a thesis record for ``ticker``.

    ``thesis_text`` is the owner's reasoning in the owner's own
    words — blank text raises ``ValueError`` because the module
    must not invent it.  The evidence snapshot is taken from the
    current pipeline outputs.  When no conditions are supplied,
    suggested defaults from the snapshot are used and labelled
    as such.  Returns the thesis dict; when ``save`` is true it
    is also written as a JSON file (see :func:`save_thesis`).
    """
    ticker = ticker.upper()
    if thesis_text is None or not str(thesis_text).strip():
        raise ValueError(
            "thesis_text is required: a thesis records the owner's "
            "reasoning in the owner's own words, and this module "
            "does not invent it")
    created_date = created_date or date.today().isoformat()
    snapshot = build_evidence_snapshot(
        conn, ticker, price_input=price_input,
        sector_override=sector_override)

    conditions_from = "caller-supplied"
    skipped_conditions = []
    if confirm_conditions is None or invalidate_conditions is None:
        default_confirm, default_invalidate, skipped_conditions = (
            default_conditions(snapshot))
        if confirm_conditions is None:
            confirm_conditions = default_confirm
        if invalidate_conditions is None:
            invalidate_conditions = default_invalidate
        conditions_from = (
            "suggested defaults from the evidence snapshot "
            "(Steps 23/24/27)")

    if snapshot["status"] == "insufficient_data":
        status = "stale"
        status_reason = (
            "no evidence snapshot could be built: "
            f"{snapshot.get('reason')}")
    else:
        status = "active"
        status_reason = (
            "thesis created with an evidence snapshot dated "
            f"{created_date}")

    universe = {}
    try:
        for row in ms.load_universe(conn):
            if row["ticker"] == ticker:
                universe = row
                break
    except Exception:  # noqa: BLE001 - header is best effort
        pass

    thesis = {
        "thesis_version": THESIS_VERSION,
        "thesis_id": None,  # set by save_thesis (needs the directory)
        "ticker": ticker,
        "company_name": universe.get("company_name"),
        "created_date": created_date,
        "author": author,
        "thesis_text": str(thesis_text).strip(),
        "assumptions": {
            "fair_value_range": snapshot["fair_value_range"],
            "tier": snapshot["tier"],
            "quality_rating": snapshot["quality_rating"],
            "quality_mean_score": snapshot["quality_mean_score"],
            "health_verdict": snapshot["health_verdict"],
            "deterioration_verdict": snapshot["deterioration_verdict"],
            "material_deterioration":
                snapshot["material_deterioration"],
            "strongest_alert_threshold_price":
                snapshot["strongest_alert_threshold_price"],
            "price": snapshot["price"],
            "price_as_of": snapshot["price_as_of"],
            "price_source": snapshot["price_source"],
            "reporting_period": snapshot["reporting_period"],
        },
        "confirm_conditions": list(confirm_conditions or []),
        "invalidate_conditions": list(invalidate_conditions or []),
        "conditions_from": conditions_from,
        "conditions_skipped_already_met": skipped_conditions,
        "evidence_snapshot": snapshot,
        "status": status,
        "status_reason": status_reason,
        "status_history": [{
            "date": created_date, "status": status,
            "reason": status_reason}],
        "reviews": [],
        "file_path": None,
    }
    if save:
        save_thesis(thesis, theses_dir)
    return thesis


def save_thesis(thesis, theses_dir=None):
    """Write a thesis dict as a JSON file; return the path.

    The file name is ``{ticker}_{created_date}_{seq}.json`` in
    ``theses_dir`` (default ``data/theses/`` under the project
    root).  The thesis dict is updated in place with its
    ``thesis_id`` and ``file_path``.
    """
    directory = Path(theses_dir) if theses_dir else DEFAULT_THESES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if not thesis.get("thesis_id"):
        thesis["thesis_id"] = _thesis_id(
            thesis["ticker"], thesis["created_date"], directory)
    path = directory / f"{thesis['thesis_id']}.json"
    thesis["file_path"] = str(path)
    path.write_text(json.dumps(thesis, indent=2), encoding="utf-8")
    return str(path)


def load_thesis(path):
    """Load a thesis JSON file; return the thesis dict."""
    with open(path, encoding="utf-8") as handle:
        thesis = json.load(handle)
    thesis["file_path"] = str(path)
    return thesis


def list_theses(ticker=None, theses_dir=None):
    """All saved theses, optionally filtered by ticker.

    Returns thesis dicts sorted by (created_date, thesis_id).
    """
    directory = Path(theses_dir) if theses_dir else DEFAULT_THESES_DIR
    if not directory.exists():
        return []
    pattern = f"{ticker.upper()}_*.json" if ticker else "*.json"
    theses = []
    for path in sorted(directory.glob(pattern)):
        try:
            theses.append(load_thesis(path))
        except Exception:  # noqa: BLE001 - skip unreadable files
            continue
    theses.sort(key=lambda t: (t.get("created_date", ""),
                               t.get("thesis_id", "")))
    return theses


def thesis_history(ticker, theses_dir=None):
    """Per-ticker thesis history: id, dates and statuses."""
    return [{
        "thesis_id": t.get("thesis_id"),
        "created_date": t.get("created_date"),
        "status": t.get("status"),
        "status_reason": t.get("status_reason"),
        "reviews": len(t.get("reviews", [])),
        "file_path": t.get("file_path"),
    } for t in list_theses(ticker, theses_dir)]


def update_thesis_status(thesis, status, reason, status_date=None):
    """Set a thesis status, recording the change in history."""
    if status not in STATUSES:
        raise ValueError(
            f"unknown thesis status '{status}'; expected one of "
            f"{', '.join(STATUSES)}")
    status_date = status_date or date.today().isoformat()
    thesis["status"] = status
    thesis["status_reason"] = reason
    thesis["status_history"].append({
        "date": status_date, "status": status, "reason": reason})
    return thesis


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

# Snapshot fields compared between creation and review.
COMPARE_FIELDS = (
    ("price", "Price"),
    ("tier", "Tier"),
    ("quality_rating", "Business quality rating"),
    ("material_deterioration", "Material deterioration"),
    ("deterioration_verdict", "Deterioration verdict"),
    ("health_verdict", "Health verdict"),
    ("strongest_alert_threshold_price", "Strongest-alert price"),
)


def _compare_snapshots(old, new):
    """Field-by-field changes between two evidence snapshots."""
    changes = []
    for field, label in COMPARE_FIELDS:
        before, after = old.get(field), new.get(field)
        if before != after:
            changes.append({"field": field, "label": label,
                            "before": before, "after": after})
    old_range = old.get("fair_value_range") or {}
    new_range = new.get("fair_value_range") or {}
    for point in ("conservative", "base", "optimistic"):
        before, after = old_range.get(point), new_range.get(point)
        if before != after:
            changes.append({
                "field": f"fair_value_range.{point}",
                "label": f"Fair value ({point})",
                "before": before, "after": after})
    old_figures = old.get("key_figures") or {}
    new_figures = new.get("key_figures") or {}
    for concept in ("revenue", "net_income", "free_cash_flow"):
        before, after = old_figures.get(concept), new_figures.get(concept)
        if before != after:
            changes.append({
                "field": f"key_figures.{concept}",
                "label": f"Latest {concept.replace('_', ' ')}",
                "before": before, "after": after})
    return changes


def review_thesis(conn, thesis, price_input=None, sector_override=None,
                  review_date=None):
    """Review a thesis against current data.

    Re-runs the pipeline, compares the new evidence snapshot
    with the one taken at creation, evaluates the confirm /
    invalidate conditions, and suggests a status.  Returns the
    review dict; it does not change the thesis — call
    :func:`apply_review` to record it.  The suggested status is
    ``invalidated`` when any invalidate condition is met,
    ``confirmed`` when every checkable confirm condition is met
    (and at least one confirm condition was checkable),
    ``stale`` when no fresh snapshot could be built, otherwise
    ``active``.
    """
    review_date = review_date or date.today().isoformat()
    snapshot = build_evidence_snapshot(
        conn, thesis["ticker"], price_input=price_input,
        sector_override=sector_override)
    old = thesis.get("evidence_snapshot") or {}
    changes = _compare_snapshots(old, snapshot)

    confirm = [evaluate_condition(c, snapshot)
               for c in thesis.get("confirm_conditions", [])]
    invalidate = [evaluate_condition(c, snapshot)
                  for c in thesis.get("invalidate_conditions", [])]
    confirm_met = [r for r in confirm if r["met"] is True]
    confirm_unchecked = [r for r in confirm if r["met"] is None]
    invalidate_met = [r for r in invalidate if r["met"] is True]

    if snapshot["status"] == "insufficient_data":
        suggested, reason = "stale", (
            "no fresh evidence snapshot could be built: "
            f"{snapshot.get('reason')}")
    elif invalidate_met:
        suggested, reason = "invalidated", (
            "invalidation condition met: " + "; ".join(
                (r["condition"].get("description") or "")
                for r in invalidate_met))
    elif confirm and confirm_met and len(confirm_met) == len(confirm) \
            and not confirm_unchecked:
        suggested, reason = "confirmed", (
            "all confirm conditions met: " + "; ".join(
                (r["condition"].get("description") or "")
                for r in confirm_met))
    elif confirm_met:
        suggested, reason = "active", (
            "some confirm conditions met (" +
            "; ".join((r["condition"].get("description") or "")
                      for r in confirm_met) +
            ") but not all")
    else:
        suggested, reason = "active", (
            "no confirm or invalidate condition met; thesis "
            "unchanged in substance" if not changes else
            "evidence changed but no confirm or invalidate "
            "condition met")

    return {
        "review_date": review_date,
        "ticker": thesis["ticker"],
        "thesis_id": thesis.get("thesis_id"),
        "snapshot": snapshot,
        "changes": changes,
        "confirm_conditions": confirm,
        "invalidate_conditions": invalidate,
        "confirm_met": len(confirm_met),
        "confirm_total": len(confirm),
        "invalidate_met": len(invalidate_met),
        "invalidate_total": len(invalidate),
        "suggested_status": suggested,
        "suggested_reason": reason,
    }


def apply_review(thesis, review, new_status=None, reason=None,
                 review_date=None):
    """Record a review on a thesis and update its status.

    ``new_status`` defaults to the review's suggested status;
    ``reason`` defaults to the review's suggested reason.  The
    review is appended to ``thesis["reviews"]`` and the status
    change is recorded in ``status_history``.  Returns the thesis.
    """
    status = new_status or review["suggested_status"]
    reason = reason or review["suggested_reason"]
    review_date = review_date or review.get("review_date") \
        or date.today().isoformat()
    thesis["reviews"].append(review)
    if status != thesis.get("status"):
        update_thesis_status(thesis, status, reason,
                             status_date=review_date)
    else:
        thesis["status_reason"] = reason
    return thesis


def thesis_summary(thesis):
    """One-line summary of a thesis for a briefing or listing."""
    snapshot = thesis.get("evidence_snapshot") or {}
    range_values = snapshot.get("fair_value_range") or {}
    price = snapshot.get("price")
    tier = snapshot.get("tier")
    quality = snapshot.get("quality_rating")
    created = thesis.get("created_date")
    text = (f"{thesis.get('ticker')} thesis ({created}): "
            f"status {thesis.get('status')}, tier {tier or 'n/a'}, "
            f"quality {quality or 'n/a'}")
    if price is not None:
        text += f", price {price:,.2f}"
    if range_values.get("conservative") is not None:
        text += (f", conservative value "
                 f"{range_values['conservative']:,.2f}")
    text += f". Reviews: {len(thesis.get('reviews', []))}."
    return text


def run_thesis_review(db_path=None, thesis=None, **kwargs):
    """Review a thesis against a database file (read-only).

    Opens the database read-only, runs :func:`review_thesis`
    and returns the review dict.  The thesis itself is not
    modified and nothing is written to the database.
    """
    import sqlite3

    if thesis is None:
        raise ValueError("thesis is required")
    db_path = db_path or str(
        Path(__file__).resolve().parent.parent / "database"
        / "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return review_thesis(conn, thesis, **kwargs)
    finally:
        conn.close()
