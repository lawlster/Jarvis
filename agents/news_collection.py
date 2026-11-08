"""Step 31 — Market news collection.

Collection only: fetch, dedupe, store, tag. No summarising, no
sentiment, no recommendations (those are Steps 32/33). No buy/sell
call anywhere; news is input to research, not a recommendation.

The default fetcher uses the ddgs news endpoint first — the same
ddgs library the project already depends on (``tools/research.py``
uses its text endpoint) — because the text endpoint returns
aggregator hub pages for a "ticker stock news" query (verified
live 2026-11-08: 5/5 hub pages, no dates), while the news endpoint
returns real dated articles. If the news endpoint fails or returns
nothing, the fetcher falls back to ``tools/research.py``'s
``search_web`` text search. The fetcher is pluggable — pass any
callable ``fetcher(query, max_results)`` returning a list of raw
result dicts in that shape, or an error string — so tests never
touch the network.

Storage is JSON files, one file per fetch run, under a
caller-chosen directory (default ``data/news/``), following the
Step 26/30 file-log pattern. Nothing in this module writes to the
production database: there is no news table, and no migration is
created here.

Per-article record fields: title, source (publisher name when the
fetcher supplies one, else the URL domain), published_date
(raw string as returned, plus ``published_date_iso`` when the raw
string parses), url, snippet, tickers (ticker symbols identified
in the title/snippet, plus the query ticker the article was
fetched for), fetched_at (UTC ISO), query (the search query that
found it).

Dedupe: by normalised URL first; where the URL is absent, by
lowercased title + source. The same article fetched twice is
stored once.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

DEFAULT_NEWS_DIR = Path(__file__).resolve().parent.parent / "data" / "news"
DEFAULT_MAX_PER_TICKER = 5
DEFAULT_DELAY_SECONDS = 1.0

# Formats ddgs / search_web have been seen returning for the date field.
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d %b %Y",
    "%b %d, %Y",
    "%Y/%m/%d",
)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_published_date(raw):
    """Parse a raw published-date string to ISO ``YYYY-MM-DD`` or None.

    Returns None for missing, unparseable or placeholder values
    (e.g. "Publication date unavailable"). The raw string is always
    kept on the record alongside; this is only the normalised form.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower().startswith("publication date"):
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def source_from_url(url):
    """Domain of a URL, lowercased, without a leading ``www.``."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def normalize_url(url):
    """Normalised URL for dedupe: lowercased host, no trailing slash.

    The path and query are kept (they distinguish articles); only
    the host case and a trailing slash are normalised, so two
    spellings of the same link collapse to one key.
    """
    if not url or not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    try:
        parts = urlparse(text)
        host = parts.netloc.lower()
        path = parts.path.rstrip("/") or "/"
        return urlunparse(
            (parts.scheme.lower(), host, path, parts.params, parts.query, "")
        )
    except Exception:
        return text.lower().rstrip("/")


def article_key(article):
    """Dedupe key: normalised URL, else lowercased title + source."""
    url = normalize_url(article.get("url", ""))
    if url:
        return ("url", url)
    return (
        "title",
        (article.get("title", "") or "").strip().lower(),
        (article.get("source", "") or "").strip().lower(),
    )


def dedupe_articles(articles):
    """Return articles with duplicates removed, first occurrence kept.

    A duplicate's ticker tags are merged into the kept article: the
    same story found for MSFT and for NVDA is one article tagged
    with both tickers, not two copies and not one tag lost.
    """
    seen = {}
    kept = []
    for article in articles:
        key = article_key(article)
        if key in seen:
            existing = seen[key]
            for symbol in article.get("tickers") or []:
                if symbol not in existing["tickers"]:
                    existing["tickers"].append(symbol)
            continue
        seen[key] = article
        kept.append(article)
    return kept


def tag_tickers(text, tickers, names=None):
    """Ticker symbols mentioned in ``text`` (word-boundary match).

    ``tickers`` is the candidate set (e.g. the tickers requested in
    this run). ``names`` optionally maps a ticker to company-name
    aliases (e.g. ``{"MSFT": ["Microsoft"]}``); an alias match tags
    the ticker too. Matching is case-insensitive; a ticker matches
    only as a whole word, so "MSFT" does not match "MSFTA".
    """
    if not text:
        return []
    haystack = text
    found = []
    for ticker in tickers:
        symbol = ticker.upper()
        if re.search(r"\b" + re.escape(symbol) + r"\b", haystack, re.IGNORECASE):
            found.append(symbol)
            continue
        for alias in (names or {}).get(symbol, []) or []:
            if alias and re.search(
                r"\b" + re.escape(alias) + r"\b", haystack, re.IGNORECASE
            ):
                found.append(symbol)
                break
    return found


def normalize_article(raw, query_ticker=None, tickers=None, names=None,
                      query=None, fetched_at=None):
    """Build a canonical article record from one raw search result.

    ``raw`` is a dict in the ``tools/research.py`` shape
    (``title``, ``url``, ``date``, ``description``; ``body`` is
    accepted as a snippet fallback). Missing fields become "" /
    None, never an exception. The query ticker is always tagged,
    plus any other requested ticker (or company-name alias)
    mentioned in the title or snippet.
    """
    raw = raw or {}
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or raw.get("href") or "").strip()
    snippet = (raw.get("description") or raw.get("body") or "").strip()
    # Publisher name when the fetcher supplies one (ddgs news
    # returns e.g. "Reuters on MSN"); otherwise the URL domain.
    source = (raw.get("source") or "").strip() or source_from_url(url)
    published_raw = raw.get("date") or raw.get("published") or ""
    published_raw = published_raw.strip() if isinstance(published_raw, str) else ""
    candidates = list(tickers or [])
    if query_ticker and query_ticker.upper() not in candidates:
        candidates.append(query_ticker.upper())
    mentioned = tag_tickers(f"{title} {snippet}", candidates, names=names)
    tagged = []
    if query_ticker and query_ticker.upper() not in tagged:
        tagged.append(query_ticker.upper())
    for symbol in mentioned:
        if symbol not in tagged:
            tagged.append(symbol)
    return {
        "title": title,
        "source": source,
        "published_date": published_raw,
        "published_date_iso": parse_published_date(published_raw),
        "url": url,
        "snippet": snippet,
        "tickers": tagged,
        "fetched_at": fetched_at or _utcnow_iso(),
        "query": query or "",
    }


def default_fetcher(query, max_results=5):
    """Default fetcher: ddgs news endpoint, text search as fallback.

    Returns a list of raw result dicts, or an error string when
    both endpoints fail (the caller records the string as the
    run's fetch error instead of raising). An empty news result
    also falls through to the text search, so a quiet news day
    still returns what the general search finds.
    """
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            news = list(ddgs.news(query, max_results=max_results))
        if news:
            return news
    except Exception:
        pass
    from tools.research import search_web

    results = search_web(query, max_results=max_results)
    if isinstance(results, str):
        return results
    return list(results) if results else []


def _run_filename(tickers, started_at):
    stamp = started_at.replace(":", "").replace("-", "").replace("T", "_")
    stamp = stamp.split(".")[0].rstrip("Z")
    slug = "_".join(t.upper() for t in tickers) or "no-tickers"
    return f"{stamp}_{slug}.json"


def collect_news(tickers, fetcher=None, news_dir=None,
                 max_per_ticker=DEFAULT_MAX_PER_TICKER,
                 delay_seconds=DEFAULT_DELAY_SECONDS,
                 names=None, query_template=None,
                 sleep_fn=None):
    """Fetch news for an explicit ticker list and store one run file.

    ``tickers`` must be an explicit list — the whole universe is
    never fetched in one run. One search query runs per ticker
    (default ``"{ticker} stock news"``, or ``query_template``
    formatted with ``{ticker}`` and ``{name}``). Between ticker
    fetches the collector sleeps ``delay_seconds`` (via
    ``sleep_fn``, default ``time.sleep`` — inject a recorder in
    tests). ``max_per_ticker`` caps the articles kept per ticker.

    Returns the run record (also written to disk as one JSON file
    under ``news_dir``). The run record carries the fetch
    parameters, per-ticker counts and errors, and the deduped
    articles, so a later reader knows exactly what was asked for.
    """
    if not tickers:
        raise ValueError("collect_news requires a non-empty ticker list")
    tickers = [t.upper() for t in tickers]
    fetcher = fetcher or default_fetcher
    sleep = sleep_fn or time.sleep
    directory = Path(news_dir) if news_dir else DEFAULT_NEWS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    started_at = _utcnow_iso()
    fetched_at = started_at
    articles = []
    per_ticker = {}
    for index, ticker in enumerate(tickers):
        if index > 0 and delay_seconds:
            sleep(delay_seconds)
        name = ""
        if names:
            aliases = names.get(ticker, []) or []
            name = aliases[0] if aliases else ""
        if query_template:
            query = query_template.format(ticker=ticker, name=name)
        elif name:
            query = f"{name} ({ticker}) stock news"
        else:
            query = f"{ticker} stock news"
        try:
            raw_results = fetcher(query, max_results=max_per_ticker)
        except Exception as error:  # fetcher failure is a run error, not a crash
            raw_results = []
            per_ticker[ticker] = {
                "query": query,
                "fetched": 0,
                "kept": 0,
                "error": f"{type(error).__name__}: {error}",
            }
            continue
        if isinstance(raw_results, str):
            per_ticker[ticker] = {
                "query": query,
                "fetched": 0,
                "kept": 0,
                "error": raw_results,
            }
            continue
        raw_list = list(raw_results or [])[:max_per_ticker]
        kept = [
            normalize_article(
                raw, query_ticker=ticker, tickers=tickers,
                names=names, query=query, fetched_at=fetched_at,
            )
            for raw in raw_list
        ]
        articles.extend(kept)
        per_ticker[ticker] = {
            "query": query,
            "fetched": len(raw_list),
            "kept": len(kept),
            "error": None,
        }

    before = len(articles)
    articles = dedupe_articles(articles)
    base_name = _run_filename(tickers, started_at)
    filename = base_name
    suffix = 2
    while (directory / filename).exists():
        filename = base_name[: -len(".json")] + f"_{suffix}.json"
        suffix += 1
    run = {
        "run_id": filename[: -len(".json")],
        "started_at": started_at,
        "finished_at": _utcnow_iso(),
        "tickers": tickers,
        "max_per_ticker": max_per_ticker,
        "delay_seconds": delay_seconds,
        "query_template": query_template,
        "fetcher": getattr(fetcher, "__name__", type(fetcher).__name__),
        "per_ticker": per_ticker,
        "fetched_total": before,
        "duplicates_removed": before - len(articles),
        "article_count": len(articles),
        "articles": articles,
    }
    path = directory / filename
    path.write_text(json.dumps(run, indent=2), encoding="utf-8")
    run["file"] = str(path)
    return run


def load_run(path):
    """Load one stored run file."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def list_runs(news_dir=None):
    """Stored run files, newest first."""
    directory = Path(news_dir) if news_dir else DEFAULT_NEWS_DIR
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"), reverse=True)


def list_articles(ticker=None, since=None, news_dir=None):
    """Stored articles, newest published date first.

    ``ticker`` filters to articles tagged with that symbol.
    ``since`` is an ISO date (``YYYY-MM-DD``); articles with no
    parseable published date are kept only when no ``since`` is
    given. Results are deduped across run files (same article
    stored in two runs is returned once).
    """
    wanted = ticker.upper() if ticker else None
    collected = []
    for path in list_runs(news_dir):
        try:
            run = load_run(path)
        except (OSError, ValueError):
            continue
        for article in run.get("articles", []):
            if wanted and wanted not in (article.get("tickers") or []):
                continue
            iso = article.get("published_date_iso")
            if since and (not iso or iso < since):
                continue
            collected.append(article)
    collected = dedupe_articles(collected)
    collected.sort(
        key=lambda a: (a.get("published_date_iso") or "", a.get("fetched_at") or ""),
        reverse=True,
    )
    return collected
