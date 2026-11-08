"""Step 32 — Daily market summaries (Phase D).

Composes the outputs of earlier steps into one structured daily
summary, as a dict and as Markdown. It derives nothing new about
value: candidates and traps come from the Step 26 detector (which
itself runs the Step 25 scanner and the Step 16-24 pipeline),
watchlist notes come from the Step 23/24/27 outputs on the scan
results, and the news digest comes from the stored Step 31
articles. Movers compare caller-supplied current prices against
caller-supplied previous prices — a price change is reported as
price data only, and a reason is stated only when a stored news
article for that ticker exists and is cited.

What this module does NOT do: it sends nothing and schedules
nothing (delivery is a later step), it makes no network requests,
it never writes to the production database, and it never makes a
buy/sell call — candidates are "worth a closer look" and the
final decision is always the owner's. The words "buy" and "sell"
do not appear anywhere in the generated summary: the Step 26
briefing lines are reused with their "(worth a closer look; not
a buy recommendation)" suffix normalised to "(worth a closer
look)".

Graceful degradation is a feature, not an afterthought: with
1 scannable ticker and 0 candidates the summary says so
plainly, with the coverage numbers, rather than padding. Every
section states what it is based on (or that it is empty and
why), and prices always carry their as-of date and source.
"""

import json
import os
import sqlite3
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path

from agents import market_scanner as ms
from agents import news_collection as nc
from agents import undervalued_detector as ud

DEFAULT_SUMMARIES_DIR = Path(__file__).resolve().parent.parent / "summaries"

# The Step 26 briefing line ends with this suffix; it is replaced
# (not quoted) so the generated summary contains no buy/sell words.
_BRIEFING_SUFFIX = " (worth a closer look; not a buy recommendation)"
_BRIEFING_REPLACEMENT = " (worth a closer look)"


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _weekday_note(summary_date):
    """Weekday name for a YYYY-MM-DD date, plus a weekend note.

    Only the calendar is claimed: Saturday/Sunday means the US
    market was closed for the weekend. Holidays are not claimed
    (they are not in the data), so a weekday carries no
    open/closed claim.
    """
    try:
        day = _date.fromisoformat(summary_date)
    except (TypeError, ValueError):
        return "", ""
    name = day.strftime("%A")
    if day.weekday() >= 5:
        return name, f"{name} — US markets closed (weekend)"
    return name, ""


def _candidate_line(record):
    """One-line candidate text, from the Step 26 briefing line.

    The briefing line is reused verbatim except for its trailing
    suffix, which is normalised so no buy/sell word appears in
    the generated summary.
    """
    line = ud.briefing_line(record)
    if line.endswith(_BRIEFING_SUFFIX):
        line = line[: -len(_BRIEFING_SUFFIX)] + _BRIEFING_REPLACEMENT
    return line


def _trap_line(trap):
    """One-line value-trap text, from the Step 26 briefing output."""
    lines = ud.briefing_summary(
        {"summary": {"candidates": 0, "strongest_alerts": 0, "watch": 0,
                     "scannable": None, "selected": None},
         "candidates": [], "value_traps": [trap]})["trap_lines"]
    return lines[0] if lines else (
        f"{trap.get('ticker')} — VALUE TRAP (not a candidate)")


def _watchlist_note(candidate):
    """One-line watchlist note from Step 23/24/27 outputs.

    ``candidate`` is one Step 25 scanner result dict. The note
    carries tier, quality, health, the Step 27 deterioration
    verdict and risk flags where they exist, margin vs the
    conservative value, and price with its as-of date — nothing
    invented, nothing recommended.
    """
    ticker = candidate.get("ticker")
    tier = candidate.get("tier")
    if tier == "insufficient_data":
        reasons = list(candidate.get("missing_inputs", []))
        return {
            "ticker": ticker,
            "company_name": candidate.get("company_name"),
            "note": (
                f"{ticker}: insufficient data — "
                + ("; ".join(reasons) if reasons else
                   "no valuation could be computed from the "
                   "available data")),
            "tier": tier,
            "quality_rating": candidate.get("quality_rating"),
            "health_verdict": candidate.get("health_verdict"),
            "price": candidate.get("price"),
            "price_as_of": candidate.get("price_as_of"),
        }
    detail = candidate.get("detail") or {}
    deterioration_27 = detail.get("deterioration_27") or {}
    overall = deterioration_27.get("overall_verdict")
    flags = deterioration_27.get("risk_flags") or []
    flag_names = [f.get("flag") for f in flags if f.get("flag")]
    margin = candidate.get("margin_of_safety_vs_conservative")
    margin_text = (f"{margin:.1%} vs conservative value"
                   if margin is not None else "margin unknown")
    price = candidate.get("price")
    price_text = (f"${price:,.2f}" if price is not None else
                  "price unknown")
    price_as_of = candidate.get("price_as_of") or "date unknown"
    parts = [f"{ticker}: tier {tier}, quality "
             f"{candidate.get('quality_rating')}, health "
             f"{candidate.get('health_verdict')}",
             f"margin {margin_text}",
             f"price {price_text} as of {price_as_of}"]
    if overall:
        parts.append(f"deterioration: {overall}")
    if flag_names:
        parts.append("risks: " + "; ".join(flag_names))
    return {
        "ticker": ticker,
        "company_name": candidate.get("company_name"),
        "note": ", ".join(parts),
        "tier": tier,
        "quality_rating": candidate.get("quality_rating"),
        "health_verdict": candidate.get("health_verdict"),
        "deterioration_overall": overall,
        "deterioration_flags": flag_names,
        "margin_of_safety_vs_conservative": margin,
        "price": price,
        "price_as_of": price_as_of,
        "price_source": candidate.get("price_source"),
    }


def _movers(scan_results, previous_prices):
    """Price movers: current (scan) price vs caller-supplied previous.

    ``previous_prices`` maps ticker -> price (number or
    price-input dict, same shape as the scanner accepts). A
    ticker appears only when both a current and a previous price
    exist. Ranked by absolute percentage change, largest first.
    The change is price data only — ``reason`` stays None unless
    the caller supplies a news article for the ticker (see
    :func:`generate_summary`, which attaches stored articles).
    """
    previous = {}
    for ticker, value in (previous_prices or {}).items():
        normalised = ms.normalise_price_input(value)
        if normalised is not None:
            previous[str(ticker).upper()] = normalised
    movers = []
    for result in scan_results:
        ticker = str(result.get("ticker") or "").upper()
        current = result.get("price")
        prev = previous.get(ticker)
        if current is None or prev is None:
            continue
        prev_price = prev["price"]
        if prev_price == 0:
            continue
        change = current - prev_price
        movers.append({
            "ticker": ticker,
            "company_name": result.get("company_name"),
            "previous_price": prev_price,
            "previous_price_as_of": prev.get("as_of_date"),
            "previous_price_source": prev.get("source"),
            "current_price": current,
            "current_price_as_of": result.get("price_as_of"),
            "current_price_source": result.get("price_source"),
            "change": change,
            "change_pct": change / prev_price,
            "reason": None,
            "reason_article": None,
        })
    movers.sort(key=lambda m: (abs(m["change_pct"]), m["ticker"]),
                reverse=True)
    return movers


def _news_digest(news_dir, summary_date, tickers):
    """Stored Step 31 articles for the summary date, grouped by ticker.

    Returns {"for_date": {ticker: [articles]}, "latest_stored":
    [articles], "article_count": n, "for_date_count": n}. An
    article is "for the date" when its parsed published date
    equals the summary date. ``latest_stored`` is the most recent
    stored articles (up to 5, newest published date first) so a
    day with no same-day articles still shows what was last
    collected — labelled as stored, not as today's news. Only
    the stored snippet is shown; nothing is summarised beyond it
    and no sentiment is attached (that is Step 33).
    """
    wanted = {str(t).upper() for t in (tickers or [])}
    articles = nc.list_articles(news_dir=news_dir)
    for_date = {}
    latest = []
    for article in articles:
        article_tickers = [str(t).upper()
                           for t in article.get("tickers") or []]
        if wanted and not (set(article_tickers) & wanted):
            continue
        latest.append(article)
        if article.get("published_date_iso") == summary_date:
            for symbol in article_tickers:
                if symbol in wanted or not wanted:
                    for_date.setdefault(symbol, []).append(article)
    latest = latest[:5]
    return {
        "for_date": for_date,
        "for_date_count": sum(len(v) for v in for_date.values()),
        "latest_stored": latest,
        "article_count": len(articles),
    }


def _upcoming_items(upcoming):
    """Normalise caller-supplied "what to watch" items.

    Items are listed exactly as given, never invented: each is
    either a string or a dict with "item" (and optional "date").
    """
    items = []
    for entry in upcoming or []:
        if isinstance(entry, str):
            items.append({"item": entry, "date": None})
        elif isinstance(entry, dict):
            items.append({
                "item": entry.get("item") or entry.get("title") or "",
                "date": entry.get("date"),
            })
    return items


def generate_summary(conn, date=None, tickers=None, prices=None,
                     previous_prices=None, upcoming=None, watchlist=None,
                     news_dir=None, detection=None, sector_overrides=None,
                     already_surfed=None):
    """Generate the daily summary dict for one date.

    ``date`` is "YYYY-MM-DD" (default: today, UTC). ``tickers``
    is an explicit ticker list; when None the whole universe is
    scanned. ``prices`` are caller-supplied current prices
    (number or price-input dict per ticker); without them the
    latest snapshot prices are used inside the pipeline, labelled
    with their as-of dates. ``previous_prices`` are the prices to
    compare against for the movers section. ``upcoming`` is the
    caller-supplied "what to watch" list. ``watchlist`` is the
    ticker list for the watchlist-notes section; when None it
    defaults to the scannable tickers in this run. ``detection``
    may be a pre-computed Step 26 detection dict (tests and
    callers that already ran it); otherwise it is run here.
    No network requests, no database writes.
    """
    summary_date = date or datetime.now(timezone.utc).date().isoformat()
    weekday, weekend_note = _weekday_note(summary_date)

    if detection is None:
        detection = ud.detect_undervalued(
            conn, tickers=tickers, prices=prices,
            sector_overrides=sector_overrides,
            already_surfed=already_surfed)
    scan = detection.get("scan") or {}
    results = scan.get("results", [])
    coverage = detection.get("coverage", {}) or scan.get("coverage", {})
    det_summary = detection.get("summary", {})

    candidates = detection.get("candidates", [])
    traps = detection.get("value_traps", [])
    candidate_lines = [_candidate_line(r) for r in candidates]
    trap_lines = [_trap_line(t) for t in traps]
    briefing = ud.briefing_summary(detection)
    headline = (
        f"Daily market summary — {summary_date}"
        + (f" ({weekday})" if weekday else "")
        + f": {coverage.get('selected', len(results))} tickers "
        f"scanned, {coverage.get('scannable', '?')} scannable, "
        f"{len(candidates)} candidate(s) "
        f"({det_summary.get('strongest_alerts', 0)} strongest "
        f"alert, {det_summary.get('watch', 0)} watch), "
        f"{len(traps)} value trap(s)")

    movers = _movers(results, previous_prices)
    # Attach stored news as the only permitted mover reason.
    digest = _news_digest(news_dir, summary_date, tickers)
    by_ticker_news = {}
    for article in digest["latest_stored"]:
        for symbol in article.get("tickers") or []:
            by_ticker_news.setdefault(str(symbol).upper(), []).append(article)
    for article_list in digest["for_date"].values():
        for article in article_list:
            for symbol in article.get("tickers") or []:
                by_ticker_news.setdefault(
                    str(symbol).upper(), []).append(article)
    for mover in movers:
        articles = by_ticker_news.get(mover["ticker"]) or []
        if articles:
            article = articles[0]
            mover["reason"] = (
                "stored news: "
                f"\"{article.get('title', '')}\" "
                f"({article.get('source', '')}, "
                f"{article.get('published_date_iso') or 'date unknown'})")
            mover["reason_article"] = {
                "title": article.get("title"),
                "source": article.get("source"),
                "published_date": article.get("published_date_iso"),
                "url": article.get("url"),
            }

    if watchlist is None:
        watchlist = [r["ticker"] for r in results if r.get("scannable")]
    by_ticker = {str(r["ticker"]).upper(): r for r in results}
    notes = []
    for ticker in watchlist:
        result = by_ticker.get(str(ticker).upper())
        if result is None:
            notes.append({
                "ticker": str(ticker).upper(),
                "company_name": None,
                "note": (f"{str(ticker).upper()}: not in this "
                         "summary's scan selection"),
                "tier": None,
                "quality_rating": None,
                "health_verdict": None,
                "price": None,
                "price_as_of": None,
            })
        else:
            notes.append(_watchlist_note(result))

    limitations = []
    selected = coverage.get("selected", len(results))
    scannable = coverage.get("scannable")
    if scannable is not None and selected and scannable < selected:
        limitations.append(
            f"only {scannable} of {selected} scanned tickers "
            f"{'is' if scannable == 1 else 'are'} scannable "
            "(statements and a price) — the rest are insufficient "
            "data (see coverage)")
    price_sources = coverage.get("price_sources") or {}
    if "financial_snapshots (latest row)" in price_sources:
        limitations.append(
            "prices come from the 2026-11-03 financial_snapshots "
            "rows unless labelled otherwise — there is no live "
            "price feed")
    if not candidates:
        limitations.append(
            "no candidate was found on the data available — this "
            "is a coverage statement, not a claim that no "
            "undervalued company exists")
    limitations.append(
        "candidates are worth a closer look, not recommendations; "
        "the final decision is always the owner's")

    return {
        "date": summary_date,
        "weekday": weekday,
        "weekend_note": weekend_note,
        "generated_at_utc": _utcnow_iso(),
        "headline": headline,
        "coverage": coverage,
        "candidates": candidates,
        "candidate_lines": candidate_lines,
        "value_traps": traps,
        "trap_lines": trap_lines,
        "briefing_headline": briefing.get("headline"),
        "movers": movers,
        "movers_available": bool(previous_prices),
        "news_digest": digest,
        "watchlist": list(watchlist or []),
        "watchlist_notes": notes,
        "upcoming": _upcoming_items(upcoming),
        "limitations": limitations,
        "warnings": list(detection.get("warnings", [])),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _md_money(value):
    return f"${value:,.2f}" if value is not None else "unknown"


def _md_pct(value):
    return f"{value:.1%}" if value is not None else "unknown"


def summary_to_markdown(summary):
    """Render a summary dict as Markdown."""
    lines = []
    date = summary["date"]
    lines.append(f"# Daily market summary — {date}")
    lines.append("")
    lines.append(summary["headline"])
    if summary.get("weekend_note"):
        lines.append("")
        lines.append(summary["weekend_note"] + ".")
    lines.append("")
    lines.append(f"Generated (UTC): {summary['generated_at_utc']}.")
    lines.append("")

    coverage = summary.get("coverage", {})
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Tickers scanned: {coverage.get('selected', '?')}")
    lines.append(f"- Scannable (statements and price): "
                 f"{coverage.get('scannable', '?')}")
    lines.append(f"- With statements: "
                 f"{coverage.get('with_statements', '?')}")
    lines.append(f"- With a price: {coverage.get('with_price', '?')}")
    by_tier = coverage.get("by_tier") or {}
    if by_tier:
        lines.append("- By tier: " + ", ".join(
            f"{tier} {count}" for tier, count in
            sorted(by_tier.items())))
    price_sources = coverage.get("price_sources") or {}
    if price_sources:
        lines.append("- Price sources: " + ", ".join(
            f"{source} ({count})" for source, count in
            sorted(price_sources.items())))
    lines.append("")

    lines.append("## Candidates — worth a closer look")
    lines.append("")
    if summary["candidate_lines"]:
        for line in summary["candidate_lines"]:
            lines.append(f"- {line}")
    else:
        lines.append("- None. No candidate was found on the data "
                     "available (see Coverage).")
    lines.append("")

    lines.append("## Value traps")
    lines.append("")
    if summary["trap_lines"]:
        for line in summary["trap_lines"]:
            lines.append(f"- {line}")
    else:
        lines.append("- None found.")
    lines.append("")

    lines.append("## Movers")
    lines.append("")
    if not summary.get("movers_available"):
        lines.append("- No price changes to compare: no previous "
                     "prices were supplied for this summary, so no "
                     "mover can be computed from stored data alone.")
    elif not summary["movers"]:
        lines.append("- No movers: no ticker has both a current and "
                     "a previous price.")
    else:
        for mover in summary["movers"]:
            lines.append(
                f"- {mover['ticker']}: "
                f"{_md_money(mover['previous_price'])} -> "
                f"{_md_money(mover['current_price'])} "
                f"({_md_pct(mover['change_pct'])})"
                + (f" — {mover['reason']}" if mover.get("reason")
                   else " — no stored news article for this ticker"))
    lines.append("")

    digest = summary.get("news_digest", {})
    lines.append("## News digest")
    lines.append("")
    for_date = digest.get("for_date") or {}
    if digest.get("for_date_count"):
        for ticker in sorted(for_date):
            lines.append(f"### {ticker} — published {date}")
            lines.append("")
            for article in for_date[ticker]:
                lines.append(
                    f"- \"{article.get('title', '')}\" — "
                    f"{article.get('source', '')} "
                    f"({article.get('published_date_iso') or 'date unknown'})")
                if article.get("url"):
                    lines.append(f"  {article.get('url')}")
                if article.get("snippet"):
                    lines.append(f"  {article.get('snippet')}")
            lines.append("")
    else:
        lines.append(f"- No stored article was published on {date}.")
        lines.append("")
    latest = digest.get("latest_stored") or []
    if latest:
        lines.append("### Latest stored articles (not today's news)")
        lines.append("")
        for article in latest:
            tickers = ", ".join(article.get("tickers") or [])
            lines.append(
                f"- \"{article.get('title', '')}\" — "
                f"{article.get('source', '')} "
                f"({article.get('published_date_iso') or 'date unknown'})"
                + (f" [{tickers}]" if tickers else ""))
            if article.get("url"):
                lines.append(f"  {article.get('url')}")
        lines.append("")

    lines.append("## Watchlist notes")
    lines.append("")
    if summary["watchlist_notes"]:
        for note in summary["watchlist_notes"]:
            lines.append(f"- {note['note']}")
    else:
        lines.append("- No watchlist tickers in this summary.")
    lines.append("")

    lines.append("## What to watch")
    lines.append("")
    if summary["upcoming"]:
        for item in summary["upcoming"]:
            if item.get("date"):
                lines.append(f"- {item['date']}: {item['item']}")
            else:
                lines.append(f"- {item['item']}")
    else:
        lines.append("- Nothing supplied for this summary.")
    lines.append("")

    lines.append("## Limitations and data notes")
    lines.append("")
    for note in summary.get("limitations", []):
        lines.append(f"- {note}")
    for warning in summary.get("warnings", []):
        lines.append(f"- Warning: {warning}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage (file output only — never the production database)
# ---------------------------------------------------------------------------

def write_summary(summary, path=None, summaries_dir=None):
    """Write the Markdown summary to a file; return the path.

    ``path`` is an explicit file path; otherwise the file is
    ``daily-summary-<date>.md`` under ``summaries_dir`` (default
    ``summaries/`` in the project). An existing file of the same
    name gets a ``_2`` suffix, like the Step 31 run files.
    """
    markdown = summary_to_markdown(summary)
    if path is not None:
        target = Path(path)
    else:
        directory = (Path(summaries_dir) if summaries_dir
                     else DEFAULT_SUMMARIES_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        base = f"daily-summary-{summary['date']}.md"
        target = directory / base
        suffix = 2
        while target.exists():
            target = directory / (base[: -len(".md")]
                                 + f"_{suffix}.md")
            suffix += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return str(target)


def generate_summary_file(db_path=None, date=None, path=None,
                          summaries_dir=None, **kwargs):
    """Dry-run entry: generate a summary for a DB file.

    Opens the database read-only (``mode=ro``) and makes no
    writes to it and no network requests. ``db_path`` defaults
    to ``database/assistant.db`` next to this package's project
    root. Returns {"summary", "markdown", "file"}.
    """
    if db_path is None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "database",
                               "assistant.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        summary = generate_summary(conn, date=date, **kwargs)
    finally:
        conn.close()
    markdown = summary_to_markdown(summary)
    file_path = write_summary(summary, path=path,
                              summaries_dir=summaries_dir)
    return {"summary": summary, "markdown": markdown, "file": file_path}
