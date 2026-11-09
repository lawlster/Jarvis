"""Step 42 — General-purpose web research.

A research layer that takes one explicit question — financial or
not ("what is the current US CPI?", "summarise the latest news
on MSFT", "explain how a DCF works") — and returns a structured
research result (dict + Markdown) built ONLY from what live web
sources actually returned:

- plan — the searches to run, named explicitly;
- fetch — through the existing fetchers, never a new one:
  ``tools/research.py::search_web`` (text) and the Step 31
  ``agents/news_collection.py::default_fetcher`` (ddgs news
  endpoint first, text search as fallback). No network code
  is written here;
- source — every finding carries its URL, source name and
  published/fetched date where available; no finding is ever
  presented without a source;
- synthesise — an extractive answer: short statements quoted
  from the fetched sources, each citing its source. There is
  no model here, so nothing is paraphrased or filled in: where
  the sources disagree or say nothing, the result says so
  plainly;
- limitations — how many sources, how fresh they are, what
  was not found.

Discipline (same as Step 31): a delay between requests, a
per-query result cap (default 5) and a per-session request cap
(default 20), all recorded in the result. Explicit questions
only — the module never fetches the whole universe and never
runs unbounded searches. A caller-supplied source allowlist is
respected where given.

No DB writes, no trades, no sending, no scheduling. Nothing
here is a recommendation: the answer is the sources' words,
cited, and any quoted title that contains a recommendation
word is the article's word, labelled as such.
"""

import json
import os
import re
import time
from urllib.parse import urlparse

# Reuse the Step 31 normalisers — do not duplicate them.
from agents.news_collection import (
    normalize_url,
    parse_published_date,
    source_from_url,
)

DEFAULT_PER_QUERY_CAP = 5
DEFAULT_SESSION_CAP = 20
DEFAULT_RATE_LIMIT_SECONDS = 1.0
DEFAULT_RESEARCH_DIR = "data/web_research"

_LIVE_ENV_VAR = "JARVIS_LIVE_WEB_RESEARCH"

# Words stripped from the front of a question to get its topic.
_LEADING_PATTERNS = [
    r"^(?:what is|what's|what are|what was|what were)\s+(?:the\s+)?",
    r"^(?:how does|how do|how is|how are)\s+",
    r"^(?:explain|describe|summarise|summarize|outline)\s+",
    r"^(?:latest|recent)\s+news\s+(?:on|about|for)\s+",
    r"^news\s+(?:on|about|for)\s+",
    r"^(?:headlines?|updates?)\s+(?:on|about|for)\s+",
    r"^(?:tell me|give me)\s+(?:the\s+)?(?:latest\s+)?",
]

_NEWS_WORDS = ("news", "headline", "headlines", "latest", "happening")
_FACT_WORDS = (
    "current", "rate", "cpi", "inflation", "price", "yield", "index",
    "unemployment", "gdp", "figure", "figures", "value", "level",
    "how much", "how many",
)
_EXPLAIN_WORDS = ("explain", "how does", "how do", "what is", "what's",
                  "what are", "meaning of", "define")


def _utcnow_iso():
    """Current UTC time as an ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "research"


def _topic(question):
    """The searchable topic of a question: leading question words stripped."""
    topic = (question or "").strip()
    for pattern in _LEADING_PATTERNS:
        stripped = re.sub(pattern, "", topic, flags=re.IGNORECASE).strip()
        if stripped and stripped != topic:
            topic = stripped
            break
    topic = topic.rstrip("?").strip()
    return topic or (question or "").strip()


def classify_question(question):
    """Classify a question as ``news`` / ``fact`` / ``explainer`` / ``general``.

    Keyword rules only — no model call. News-shaped questions
    (latest news, headlines) go to the news endpoint first;
    fact questions (a current figure) and explainers go to text
    search; anything else is ``general`` (text search).
    """
    text = (question or "").strip().lower()
    if not text:
        return "general"
    if any(word in text for word in ("news", "headline")) or (
        "latest" in text and any(word in text for word in ("on", "about", "for"))
    ):
        return "news"
    # Fact words win over the generic "what is": "what is the current
    # US CPI?" asks for a figure, while "what is a DCF?" (no fact
    # word) is an explainer.
    if any(word in text for word in _FACT_WORDS):
        return "fact"
    if any(word in text for word in _EXPLAIN_WORDS):
        return "explainer"
    return "general"


def plan_research(question):
    """Produce the explicit search plan for one question.

    Returns a plan dict: the question, its kind, its topic, and
    the searches to run — each naming its query, its kind
    (``news`` or ``text``) and its purpose. No fetching happens
    here, so the plan can be inspected before anything runs.
    """
    question = (question or "").strip()
    kind = classify_question(question)
    topic = _topic(question)
    searches = []
    if kind == "news":
        searches.append({
            "query": topic,
            "kind": "news",
            "purpose": "latest news on the topic",
        })
        searches.append({
            "query": f"{topic} news",
            "kind": "text",
            "purpose": "text-search fallback phrasing for the same topic",
        })
    elif kind == "fact":
        searches.append({
            "query": question,
            "kind": "text",
            "purpose": "current figure, as asked",
        })
        if "latest" not in question.lower():
            searches.append({
                "query": f"latest {topic}",
                "kind": "text",
                "purpose": "freshest published figure for the topic",
            })
    elif kind == "explainer":
        searches.append({
            "query": question,
            "kind": "text",
            "purpose": "explanation, as asked",
        })
    else:
        searches.append({
            "query": question,
            "kind": "text",
            "purpose": "general question, as asked",
        })
    return {
        "question": question,
        "kind": kind,
        "topic": topic,
        "searches": searches,
    }


def default_text_fetcher(query, max_results=DEFAULT_PER_QUERY_CAP):
    """Text-search fetcher: the existing ``tools/research.py`` search.

    Returns ``(results, error)`` — a list of raw result dicts
    and ``None``, or ``([], error_string)`` when the search
    fails. The caller records the error instead of raising.
    """
    from tools.research import search_web

    try:
        results = search_web(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 - record, never crash
        return [], f"text search failed: {exc}"
    if isinstance(results, str):
        return [], results
    return list(results or []), None


def default_news_fetcher(query, max_results=DEFAULT_PER_QUERY_CAP):
    """News fetcher: the Step 31 default fetcher (ddgs news first,
    text search as fallback). Returns ``(results, error)`` in the
    same shape as :func:`default_text_fetcher`."""
    from agents.news_collection import default_fetcher

    try:
        results = default_fetcher(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 - record, never crash
        return [], f"news fetch failed: {exc}"
    if isinstance(results, str):
        return [], results
    return list(results or []), None


def normalize_finding(raw, query=None, fetched_at=None):
    """Build a canonical finding from one raw search result.

    The raw dict is in the ``tools/research.py`` shape
    (``title``, ``url``/``href``, ``date``/``published``,
    ``description``/``body``). Missing fields become "" / None,
    never an exception. A finding without a URL is dropped by
    the caller (it cannot be cited).
    """
    raw = raw or {}
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or raw.get("href") or "").strip()
    snippet = (raw.get("description") or raw.get("body") or "").strip()
    source = (raw.get("source") or "").strip() or source_from_url(url)
    published_raw = raw.get("date") or raw.get("published") or ""
    published_raw = (
        published_raw.strip() if isinstance(published_raw, str) else ""
    )
    return {
        "title": title,
        "url": url,
        "source": source,
        "published_date": published_raw,
        "published_date_iso": parse_published_date(published_raw),
        "snippet": snippet,
        "query": query or "",
        "fetched_at": fetched_at or _utcnow_iso(),
    }


def _domain_of(url):
    return urlparse(url or "").netloc.lower()


def _allowed(finding, allowed_sources):
    """True when a finding's source is on the caller-supplied allowlist.

    The allowlist holds domains or source names (lowercased);
    a finding passes when its domain or its source name
    contains any entry. No allowlist means everything passes.
    """
    if not allowed_sources:
        return True
    allowed = [str(a).lower() for a in allowed_sources if str(a).strip()]
    if not allowed:
        return True
    haystack = f"{_domain_of(finding.get('url'))} {finding.get('source') or ''}".lower()
    return any(entry in haystack for entry in allowed)


def _dedupe_findings(findings):
    """Drop duplicate findings by normalised URL, then by title+source."""
    seen_urls = set()
    seen_titles = set()
    kept = []
    for finding in findings:
        url = normalize_url(finding.get("url") or "")
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        key = (finding.get("title") or "").lower()
        key = f"{key}|{(finding.get('source') or '').lower()}"
        if key in seen_titles:
            continue
        seen_titles.add(key)
        kept.append(finding)
    return kept


_NUMBER_UNIT_RE = re.compile(
    r"(?P<number>-?\d[\d,]*\.?\d*)\s*(?P<unit>%|percent|per cent|"
    r"basis points?|bps|\$|dollars?|USD|trillion|billion|million)",
    re.IGNORECASE,
)


def _detect_disagreements(findings, kind):
    """Heuristic disagreement detection, labelled as such.

    Only for ``fact`` questions: extract (number, unit) pairs
    from each finding's snippet and title, group by unit, and
    report any unit that appears with two or more distinct
    values across different sources. The result is a flag to
    check the sources, not a verdict — the values may describe
    different dates, different measures, or a stale source.
    """
    if kind != "fact":
        return []
    by_unit = {}
    for idx, finding in enumerate(findings):
        text = f"{finding.get('title') or ''} {finding.get('snippet') or ''}"
        for match in _NUMBER_UNIT_RE.finditer(text):
            unit = match.group("unit").lower()
            unit = {"percent": "%", "per cent": "%", "basis point": "bps",
                    "basis points": "bps", "dollars": "$", "dollar": "$",
                    "usd": "$"}.get(unit, unit)
            try:
                value = float(match.group("number").replace(",", ""))
            except ValueError:
                continue
            by_unit.setdefault(unit, []).append(
                (value, idx, finding.get("source") or "", finding.get("url") or "")
            )
    disagreements = []
    for unit, entries in by_unit.items():
        values = {}
        for value, idx, source, url in entries:
            values.setdefault(value, []).append(
                {"source": source, "url": url, "finding": idx + 1}
            )
        if len(values) > 1:
            disagreements.append({
                "unit": unit,
                "values": [
                    {"value": value, "reported_by": reporters}
                    for value, reporters in sorted(values.items())
                ],
                "note": ("heuristic: different sources report different "
                         "figures for the same unit — check the sources; "
                         "they may describe different dates or measures"),
            })
    return disagreements


def _claims_from_findings(findings):
    """One sourced claim per finding — the finding's title and
    snippet, quoted, citing its source. Nothing is paraphrased
    and nothing is added: the answer is the sources' words."""
    claims = []
    for idx, finding in enumerate(findings, start=1):
        text = finding.get("title") or ""
        snippet = finding.get("snippet") or ""
        if snippet and snippet not in text:
            text = f"{text} — {snippet}" if text else snippet
        claims.append({
            "claim": text,
            "source_number": idx,
            "source": finding.get("source") or "",
            "url": finding.get("url") or "",
            "published_date": finding.get("published_date") or "",
        })
    return claims


def research(question, text_fetcher=None, news_fetcher=None,
             per_query_cap=DEFAULT_PER_QUERY_CAP,
             session_cap=DEFAULT_SESSION_CAP,
             rate_limit_seconds=DEFAULT_RATE_LIMIT_SECONDS,
             allowed_sources=None, sleep_fn=None, research_dir=None):
    """Research one question and return the structured result.

    ``text_fetcher`` / ``news_fetcher`` are callables taking
    ``(query, max_results)`` and returning ``(results, error)``;
    the defaults are :func:`default_text_fetcher` and
    :func:`default_news_fetcher`. Pass stub fetchers in tests —
    this function never touches the network itself.

    Caps: at most ``per_query_cap`` results per search and at
    most ``session_cap`` requests in the session; a plan with
    more searches than the cap is truncated and the truncation
    is recorded. ``rate_limit_seconds`` is slept (via
    ``sleep_fn``, default :func:`time.sleep`) between
    consecutive requests; the sleeps are recorded, never hidden.

    The result carries the plan, every search with its status
    and error, the deduplicated findings (each with URL, source
    and dates), the extractive answer (one sourced claim per
    finding), any heuristic disagreements, and a limitations
    section. Status is ``ok`` with at least one finding,
    ``partial`` when some searches failed or were truncated,
    and ``no_sources`` when nothing was found at all.
    """
    question = (question or "").strip()
    plan = plan_research(question)
    sleep = sleep_fn or time.sleep
    text_fetch = text_fetcher or default_text_fetcher
    news_fetch = news_fetcher or default_news_fetcher

    searches = list(plan["searches"])
    truncated = 0
    if len(searches) > session_cap:
        truncated = len(searches) - session_cap
        searches = searches[:session_cap]

    search_records = []
    findings = []
    requests_made = 0
    sleeps = 0
    failed_searches = 0
    empty_searches = 0
    for position, search in enumerate(searches):
        if position > 0 and rate_limit_seconds:
            sleep(rate_limit_seconds)
            sleeps += 1
        fetcher = news_fetch if search["kind"] == "news" else text_fetch
        try:
            raw_results, error = fetcher(
                search["query"], max_results=per_query_cap)
        except Exception as exc:  # noqa: BLE001 - record, never crash
            raw_results, error = [], f"fetcher raised: {exc}"
        requests_made += 1
        fetched_at = _utcnow_iso()
        record = {
            "query": search["query"],
            "kind": search["kind"],
            "purpose": search["purpose"],
            "status": "ok" if not error else "failed",
            "error": error,
            "results_returned": len(raw_results or []),
            "fetched_at": fetched_at,
        }
        if error:
            failed_searches += 1
            search_records.append(record)
            continue
        if not raw_results:
            empty_searches += 1
        for raw in raw_results or []:
            finding = normalize_finding(raw, query=search["query"],
                                        fetched_at=fetched_at)
            if not finding["url"]:
                continue
            findings.append(finding)
        search_records.append(record)

    dropped_allowlist = 0
    if allowed_sources:
        kept = []
        for finding in findings:
            if _allowed(finding, allowed_sources):
                kept.append(finding)
            else:
                dropped_allowlist += 1
        findings = kept

    findings = _dedupe_findings(findings)
    claims = _claims_from_findings(findings)
    disagreements = _detect_disagreements(findings, plan["kind"])

    dated = [f["published_date_iso"] for f in findings
             if f.get("published_date_iso")]
    sources = []
    for idx, finding in enumerate(findings, start=1):
        sources.append({
            "number": idx,
            "title": finding.get("title") or "",
            "source": finding.get("source") or "",
            "url": finding.get("url") or "",
            "published_date": finding.get("published_date") or "",
        })
    answer_text = "\n".join(
        f"[{claim['source_number']}] {claim['claim']} "
        f"({claim['source']})"
        for claim in claims
    )

    limitations = []
    limitations.append(f"{len(findings)} source(s) used.")
    if dated:
        limitations.append(
            f"freshest source dated {max(dated)}; oldest dated {min(dated)}.")
    else:
        limitations.append(
            "no source carried a parseable published date, so freshness "
            "could not be established.")
    if empty_searches:
        limitations.append(
            f"{empty_searches} search(es) returned no results.")
    if failed_searches:
        limitations.append(
            f"{failed_searches} search(es) failed — see the search records.")
    if truncated:
        limitations.append(
            f"session cap of {session_cap} request(s) truncated "
            f"{truncated} planned search(es).")
    if dropped_allowlist:
        limitations.append(
            f"{dropped_allowlist} finding(s) dropped by the source allowlist.")
    if disagreements:
        limitations.append(
            "sources disagree on at least one figure — see the "
            "disagreements section; check the sources before relying "
            "on any single figure.")
    if not findings:
        limitations.append(
            "no sources were found, so no answer could be drawn from "
            "the web — the question is unanswered, not answered "
            "from memory.")

    # A search that returns nothing is noted in the limitations,
    # not a failure — it is the failed or truncated searches that
    # make a result partial. A run whose searches all failed is
    # partial (attempted, nothing retrieved), not "no sources":
    # failure means we never learned whether sources exist.
    if findings:
        status = "partial" if (failed_searches or truncated) else "ok"
    elif failed_searches:
        status = "partial"
    else:
        status = "no_sources"

    return {
        "question": question,
        "kind": plan["kind"],
        "topic": plan["topic"],
        "status": status,
        "plan": plan,
        "searches": search_records,
        "requests_made": requests_made,
        "rate_limit_seconds": rate_limit_seconds,
        "sleeps_between_requests": sleeps,
        "per_query_cap": per_query_cap,
        "session_cap": session_cap,
        "truncated_searches": truncated,
        "allowed_sources": list(allowed_sources or []),
        "dropped_by_allowlist": dropped_allowlist,
        "findings": findings,
        "sources": sources,
        "claims": claims,
        "answer": claims,
        "answer_text": answer_text,
        "disagreements": disagreements,
        "limitations": limitations,
        "researched_at": _today_iso(),
    }


def research_to_markdown(result):
    """Render a research result as Markdown.

    The answer is the sources' words, cited by number; quoted
    titles are the articles' words, labelled as such in the
    sources section. No recommendation is added by this module.
    """
    result = result or {}
    lines = [f"# Web research: {result.get('question') or ''}", ""]
    lines.append(f"Kind: {result.get('kind') or 'general'} | "
                 f"Status: {result.get('status') or 'unknown'} | "
                 f"Researched: {result.get('researched_at') or ''}")
    lines.append("")
    lines.append("## Plan")
    lines.append("")
    for search in (result.get("plan") or {}).get("searches", []):
        lines.append(f"- {search.get('kind')}: \"{search.get('query')}\" "
                     f"— {search.get('purpose')}")
    lines.append("")
    lines.append("## Searches run")
    lines.append("")
    for record in result.get("searches", []):
        line = (f"- {record.get('kind')}: \"{record.get('query')}\" — "
                f"{record.get('status')}")
        if record.get("results_returned") is not None:
            line += f" ({record['results_returned']} result(s))"
        if record.get("error"):
            line += f" — error: {record['error']}"
        lines.append(line)
    lines.append(
        f"Requests made: {result.get('requests_made', 0)} "
        f"(session cap {result.get('session_cap')}); "
        f"rate-limit delay {result.get('rate_limit_seconds')}s between "
        f"requests, {result.get('sleeps_between_requests', 0)} sleep(s).")
    lines.append("")
    lines.append("## Answer (the sources' words, cited)")
    lines.append("")
    if result.get("answer_text"):
        lines.append(result["answer_text"])
    else:
        lines.append("No answer — no sources were found.")
    lines.append("")
    if result.get("disagreements"):
        lines.append("## Disagreements between sources")
        lines.append("")
        for item in result["disagreements"]:
            lines.append(f"- Unit: {item.get('unit')}")
            for value in item.get("values", []):
                reporters = ", ".join(
                    r.get("source") or "" for r in value.get("reported_by", []))
                lines.append(f"  - {value.get('value')}: {reporters}")
            lines.append(f"  {item.get('note') or ''}")
        lines.append("")
    lines.append("## Sources")
    lines.append("")
    if result.get("sources"):
        for source in result["sources"]:
            date = source.get("published_date") or "date unavailable"
            lines.append(f"[{source.get('number')}] {source.get('title')} — "
                         f"{source.get('source')} ({date})")
            lines.append(f"    {source.get('url')}")
    else:
        lines.append("No sources.")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in result.get("limitations", []):
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def save_research(result, research_dir=None):
    """Save a research result as JSON + Markdown under ``research_dir``
    (default ``data/web_research/``). Returns ``(json_path, md_path)``.
    The directory is created when missing. Nothing is written to
    the production database."""
    research_dir = research_dir or DEFAULT_RESEARCH_DIR
    os.makedirs(research_dir, exist_ok=True)
    stamp = _utcnow_iso().replace(":", "").replace("-", "").replace("T", "_")
    stamp = stamp.split(".")[0].rstrip("Z")
    base = f"{stamp}_{_slug(result.get('question') or 'research')}"
    json_path = os.path.join(research_dir, f"{base}.json")
    md_path = os.path.join(research_dir, f"{base}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(research_to_markdown(result))
    return json_path, md_path
