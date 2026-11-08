"""Collect official US exchange listing evidence into isolated local snapshots."""

import argparse
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import requests

if __package__:
    from . import us_security_classifier as classifier
else:
    import us_security_classifier as classifier


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = BASE_DIR / "data" / "market_data" / "us_security_evidence_cache"
DEFAULT_OUTPUT_DIR = classifier.PHASE2_OUTPUT_DIR
NASDAQ_DIRECTORY_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
NYSE_FILTER_URL = "https://www.nyse.com/api/quotes/filter"
NYSE_CATEGORIES = (
    "EQUITY",
    "EXCHANGE_TRADED_FUND",
    "PREFERRED_STOCK",
    "WARRANT",
    "RIGHT",
    "TRUST",
    "BOND",
    "REIT",
)
NASDAQ_EXCHANGES = {"Q": "NASDAQ", "N": "NYSE"}
NYSE_MIC_EXCHANGES = {
    "XNYS": "NYSE",
}
RECONCILIATION_MICS = {"XASE", "ARCX", "XNGS"}
NYSE_CATEGORY_ROUTES = {
    "EQUITY": "stock",
    "EXCHANGE_TRADED_FUND": "etf",
    "PREFERRED_STOCK": "preferred-stock",
    "WARRANT": "warrant",
    "RIGHT": "right",
    "TRUST": "trust",
    "BOND": "bond",
    "REIT": "reit",
}
NASDAQ_EVIDENCE_REFERENCE = "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs"


class RequestRateLimiter:
    """Enforce a minimum interval between requests made by one collector."""

    def __init__(self, min_interval=1.0, clock=time.monotonic, sleeper=time.sleep):
        self.min_interval = float(min_interval)
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_at = None

    def wait(self):
        now = self.clock()
        if self.last_request_at is not None:
            remaining = self.min_interval - (now - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self.last_request_at = now


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_retry_after(value, now=None):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - (now or _utc_now())).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class SnapshotCache:
    """Keep immutable content-addressed source snapshots and a latest pointer."""

    def __init__(self, cache_dir, max_age=timedelta(hours=24), now=_utc_now):
        self.cache_dir = Path(cache_dir)
        self.max_age = max_age
        self.now = now

    def read_fresh(self, source_id):
        latest_path = self.cache_dir / f"{source_id}.latest.json"
        if not latest_path.exists():
            return None
        try:
            metadata = json.loads(latest_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(metadata["fetched_at_utc"])
            snapshot_path = self.cache_dir / metadata["snapshot_file"]
            if self.now() - fetched_at > self.max_age or not snapshot_path.is_file():
                return None
            content = snapshot_path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != metadata.get("sha256"):
                return None
            return content, metadata
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def store(self, source_id, source_url, content, media_type, request_parameters=None):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        fetched_at = self.now().astimezone(timezone.utc)
        stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
        suffix = ".json" if media_type == "json" else ".txt"
        snapshot_name = f"{source_id}_{stamp}_{digest[:12]}{suffix}"
        snapshot_path = self.cache_dir / snapshot_name
        if not snapshot_path.exists():
            with snapshot_path.open("xb") as snapshot:
                snapshot.write(content)
        metadata = {
            "source_id": source_id,
            "source_url": source_url,
            "fetched_at_utc": fetched_at.isoformat(),
            "sha256": digest,
            "snapshot_file": snapshot_name,
            "media_type": media_type,
            "request_method": "POST" if request_parameters is not None else "GET",
            "request_parameters": request_parameters or {},
        }
        latest_path = self.cache_dir / f"{source_id}.latest.json"
        temporary_path = latest_path.with_suffix(".latest.json.tmp")
        temporary_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(latest_path)
        return content, metadata


class OfficialExchangeCollector:
    """Download official Nasdaq Trader and NYSE public directory records."""

    def __init__(
        self,
        cache_dir=DEFAULT_CACHE_DIR,
        session=None,
        min_interval=1.0,
        sleeper=time.sleep,
        clock=time.monotonic,
        now=_utc_now,
        retries=4,
        cache_max_age=timedelta(hours=24),
    ):
        self.cache = SnapshotCache(cache_dir, cache_max_age, now)
        self.session = session or requests.Session()
        self.limiter = RequestRateLimiter(min_interval, clock, sleeper)
        self.sleeper = sleeper
        self.now = now
        self.retries = retries
        self.session.headers.update({
            "User-Agent": "Jarvis US Security Evidence Collector/1.0",
            "Accept": "text/plain, application/json",
        })

    def _request(self, method, url, payload=None):
        headers = {}
        if method == "POST":
            headers.update({
                "Content-Type": "application/json",
                "Origin": "https://www.nyse.com",
                "Referer": self.session.headers.get(
                    "Referer", "https://www.nyse.com/listings_directory/stock"
                ),
            })
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                if method == "POST":
                    response = self.session.post(url, json=payload, headers=headers, timeout=30)
                else:
                    response = self.session.get(url, timeout=30)
            except requests.RequestException:
                if attempt >= self.retries:
                    raise
                self.sleeper(min(2 ** attempt, 30))
                continue
            status = response.status_code
            if status == 200:
                return response.content
            if status == 429 or status >= 500:
                if attempt >= self.retries:
                    response.raise_for_status()
                delay = _parse_retry_after(response.headers.get("Retry-After"), self.now())
                self.sleeper(delay if delay is not None else min(2 ** attempt, 30))
                continue
            response.raise_for_status()
        raise RuntimeError(f"Request failed after retries: {url}")

    def _fetch_snapshot(self, source_id, url, method="GET", payload=None, refresh=False):
        if not refresh:
            cached = self.cache.read_fresh(source_id)
            if cached:
                content, metadata = cached
                metadata = dict(metadata)
                metadata.setdefault("request_method", method)
                metadata.setdefault("request_parameters", payload or {})
                metadata.setdefault("source_url", url)
                metadata.setdefault("source_id", source_id)
                return content, metadata
        content = self._request(method, url, payload)
        media_type = "json" if method == "POST" else "text"
        return self.cache.store(
            source_id,
            url,
            content,
            media_type,
            request_parameters=payload,
        )

    def fetch_nasdaq(self, refresh=False):
        content, metadata = self._fetch_snapshot(
            "nasdaqtraded", NASDAQ_DIRECTORY_URL, refresh=refresh
        )
        return parse_nasdaq_directory(content.decode("utf-8-sig")), metadata

    def fetch_nyse(self, refresh=False):
        results = {}
        for category in NYSE_CATEGORIES:
            referer = "https://www.nyse.com/listings_directory/" + NYSE_CATEGORY_ROUTES[category]
            payload = {
                "instrumentType": category,
                "pageNumber": 1,
                "sortColumn": "NORMALIZED_TICKER",
                "sortOrder": "ASC",
                "maxResultsPerPage": 10000,
                "filterToken": "",
            }
            self.session.headers["Referer"] = referer
            content, metadata = self._fetch_snapshot(
                "nyse_" + category.lower(),
                NYSE_FILTER_URL,
                method="POST",
                payload=payload,
                refresh=refresh,
            )
            rows = json.loads(content.decode("utf-8-sig"))
            if not isinstance(rows, list):
                raise ValueError(f"NYSE {category} response was not a JSON array.")
            totals = {int(row.get("total", 0)) for row in rows if isinstance(row, dict)}
            if len(totals) > 1:
                raise ValueError(f"NYSE {category} response contains inconsistent total counts.")
            total = next(iter(totals), 0)
            if total and len(rows) < total:
                raise ValueError(
                    f"NYSE {category} returned {len(rows)} of {total} rows; refusing incomplete snapshot."
                )
            results[category] = {"rows": rows, "metadata": metadata}
        return results


def parse_nasdaq_directory(text):
    """Parse Nasdaq Trader's current combined symbol directory."""

    records = []
    reader = csv.DictReader(StringIO(text), delimiter="|")
    required = {"Symbol", "Security Name", "Listing Exchange", "ETF", "Test Issue"}
    missing = required.difference(reader.fieldnames or [])
    if missing:
        raise ValueError("Nasdaq directory is missing fields: " + ", ".join(sorted(missing)))
    for row in reader:
        symbol = str(row.get("Symbol") or "").strip()
        exchange_code = str(row.get("Listing Exchange") or "").strip().upper()
        if not symbol or symbol.startswith("FILE CREATION TIME"):
            continue
        exchange = NASDAQ_EXCHANGES.get(exchange_code)
        if not exchange or str(row.get("Test Issue") or "").strip().upper() == "Y":
            continue
        records.append({
            "ticker": symbol,
            "exchange": exchange,
            "security_name": str(row.get("Security Name") or "").strip(),
            "share_class": parse_share_class_description(row.get("Security Name")),
            "etf": str(row.get("ETF") or "").strip().upper() == "Y",
            "raw": row,
        })
    return records


def _exchange_ticker_from_nyse_row(row):
    quote_url = str(row.get("url") or "")
    match = re.search(r"/quote/([A-Z0-9]+):([^/?#]+)", quote_url, re.IGNORECASE)
    if match:
        mic, ticker = match.group(1).upper(), match.group(2)
        exchange = NYSE_MIC_EXCHANGES.get(mic)
        if exchange:
            return ticker.strip(), exchange
    ticker = str(row.get("symbolExchangeTicker") or row.get("normalizedTicker") or "").strip()
    if ticker:
        return ticker, ""
    return "", ""


def parse_issue_form_description(description):
    """Map only explicit instrument descriptors in an exchange issue-name field."""

    value = re.sub(r"\s+", " ", str(description or "").strip()).upper()
    if re.search(r"\bAMERICAN DEPOSITARY (?:SHARES?|RECEIPTS?)\b", value) or re.search(r"\bADS\b|\bADR\b", value):
        return "adr_ads", "Official issue description explicitly says American Depositary Shares/Receipts."
    # A listed common-share designation at the end describes the security.
    # Do this before looking for words such as Bond that may belong to the fund name.
    if re.search(r"(?:^|\b)(?:COMMON STOCK|COMMON SHARES?|ORDINARY SHARES?)\s*$", value):
        return "common_share", "Official issue description ends in an explicit common/ordinary share designation."
    if re.search(r"\b(?:PREFERRED|PREFERENCE) (?:STOCK|SHARES?)\b", value) or re.search(r"\bPFD(?:\s+(?:SER(?:IES)?\b|\d)|$)", value):
        return "preferred_share", "Official issue description explicitly identifies preferred stock/shares."
    if re.search(r"\b(?:WARRANTS?|WTS)\b", value):
        return "warrant", "Official issue description explicitly identifies warrants."
    if re.search(r"\bRIGHTS?\b", value):
        return "rights", "Official issue description explicitly identifies rights."
    if re.search(r"\b(?:CORPORATE )?UNITS?\b", value):
        return "unit", "Official issue description explicitly identifies units."
    if re.search(r"\b(?:NOTES?|DEBENTURES?)\b", value) or re.search(r"\bBONDS?\b(?!\s+FUND\b)", value):
        return "debt_note", "Official issue description explicitly identifies a debt instrument."
    return "unknown", "Official issue description contains no recognized explicit security-form descriptor."


def parse_share_class_description(description):
    """Extract explicit class/series identifiers from an official issue description."""

    value = re.sub(r"\s+", " ", str(description or "").strip()).upper()
    labels = []
    class_match = re.search(r"\b(?:CLASS|CL\.?)[\s-]+([A-Z0-9]{1,3})\b", value)
    series_match = re.search(r"\bSERIES\s+([A-Z0-9]{1,3})\b", value)
    if class_match and class_match.group(1) not in {"ORD", "COM", "COMMON", "ORDINARY"}:
        labels.append("Class " + class_match.group(1))
    if series_match:
        labels.append("Series " + series_match.group(1))
    return "; ".join(labels)


def parse_nyse_records(source_results):
    """Flatten NYSE directory filters while retaining exchange category provenance."""

    records = []
    category_forms = {
        "EXCHANGE_TRADED_FUND": ("etf", "exchange_traded_product"),
        "PREFERRED_STOCK": ("unknown", "preferred_share"),
        "WARRANT": ("unknown", "warrant"),
        "RIGHT": ("unknown", "rights"),
        "BOND": ("unknown", "debt_note"),
        "REIT": ("reit", "unknown"),
        "EQUITY": ("unknown", "unknown"),
        "TRUST": ("unknown", "unknown"),
    }
    for category, source in source_results.items():
        structure, form = category_forms[category]
        metadata = source["metadata"]
        reference = "https://www.nyse.com/api/quotes/filter"
        directory_reference = (
            "https://www.nyse.com/listings_directory/"
            + NYSE_CATEGORY_ROUTES[category]
        )
        for row in source["rows"]:
            if not isinstance(row, dict):
                continue
            ticker, exchange = _exchange_ticker_from_nyse_row(row)
            if not ticker or not exchange:
                continue
            row_structure, row_form = structure, form
            rationale = f"NYSE Listings Directory category={category}."
            name = str(row.get("instrumentName") or "").strip()
            if category in {"EQUITY", "TRUST"}:
                parsed_form, parsed_rationale = parse_issue_form_description(name)
                if parsed_form != "unknown":
                    row_form = parsed_form
                    rationale += " " + parsed_rationale
                else:
                    rationale += " " + parsed_rationale
            records.append({
                "ticker": ticker,
                "exchange": exchange,
                "issuer_structure": row_structure,
                "security_form": row_form,
                "share_class": parse_share_class_description(name),
                "source_authority": "EXCHANGE",
                "evidence_source": f"NYSE Listings Directory ({category})",
                "evidence_reference": reference,
                "directory_reference": directory_reference,
                "evidence_date": metadata["fetched_at_utc"][:10],
                "rationale": rationale,
                "source_category": category,
                "source_issue_name": name,
                "source_metadata": metadata,
            })
    return records


def build_evidence_rows(listings, nasdaq_records, nyse_records):
    """Join official sources on ticker+exchange; CIK is carried only as an integrity check."""

    listings_by_key = {}
    for listing in listings:
        listings_by_key.setdefault(classifier.listing_key(listing), []).append(listing)
    evidence = []
    unmatched = {"nasdaq": 0, "nyse": 0}

    for record in nasdaq_records:
        key = classifier.listing_key(record)
        matching_listings = listings_by_key.get(key, [])
        if not matching_listings:
            unmatched["nasdaq"] += 1
            continue
        if record["etf"]:
            structure, form, rationale = (
                "etf", "exchange_traded_product", "Nasdaq Trader ETF field is Y."
            )
        else:
            form, rationale = parse_issue_form_description(record["security_name"])
            structure = "unknown"
            rationale = "Nasdaq Trader nasdaqtraded.txt Security Name: " + rationale
        for listing in matching_listings:
            evidence.append(classifier.normalize_evidence({
                "ticker": record["ticker"],
                "exchange": record["exchange"],
                "sec_cik": listing.get("sec_cik"),
                "issuer_structure": structure,
                "security_form": form,
                "share_class": record["share_class"],
                "source_authority": "EXCHANGE",
                "evidence_source": "Nasdaq Trader nasdaqtraded.txt",
                "evidence_reference": NASDAQ_EVIDENCE_REFERENCE,
                "evidence_date": record["source_metadata"]["fetched_at_utc"][:10],
                "rationale": rationale,
            }))

    for record in nyse_records:
        key = classifier.listing_key(record)
        matching_listings = listings_by_key.get(key, [])
        if not matching_listings:
            unmatched["nyse"] += 1
            continue
        for listing in matching_listings:
            evidence.append(classifier.normalize_evidence({
                "ticker": record["ticker"],
                "exchange": record["exchange"],
                "sec_cik": listing.get("sec_cik"),
                "issuer_structure": record["issuer_structure"],
                "security_form": record["security_form"],
                "share_class": record["share_class"],
                "source_authority": record["source_authority"],
                "evidence_source": record["evidence_source"],
                "evidence_reference": record["evidence_reference"],
                "evidence_date": record["evidence_date"],
                "rationale": record["rationale"],
            }))
    return evidence, unmatched


def _write_json_new(path, value):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing Phase 2 output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


def _write_evidence_csv(path, evidence_rows):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing Phase 2 evidence file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=classifier.EVIDENCE_FIELDS)
        writer.writeheader()
        writer.writerows(evidence_rows)


def _validate_cache_dir(cache_dir, output_dir):
    cache_path = Path(cache_dir).resolve()
    protected = (
        classifier.US_DATA_DIR,
        classifier.HKEX_DATA_DIR,
        classifier.DEFAULT_OUTPUT_DIR,
        classifier.PRIOR_PHASE2_OUTPUT_DIR,
        classifier.PILOT_OUTPUT_DIR,
        classifier.DATABASE_PATH,
        output_dir,
    )
    classifier.validate_output_dir(cache_path, protected)
    return cache_path


def run_phase2(
    database_path=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    cache_dir=DEFAULT_CACHE_DIR,
    refresh=False,
    collector=None,
):
    output_dir = classifier.validate_output_dir(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Refusing to use existing versioned output directory: {output_dir}")
    cache_dir = _validate_cache_dir(cache_dir, output_dir)
    listings = classifier.load_us_listings(database_path)
    collector = collector or OfficialExchangeCollector(cache_dir=cache_dir)
    nasdaq_records, nasdaq_metadata = collector.fetch_nasdaq(refresh=refresh)
    for record in nasdaq_records:
        record["source_metadata"] = nasdaq_metadata
    nyse_source = collector.fetch_nyse(refresh=refresh)
    nyse_records = parse_nyse_records(nyse_source)
    evidence, unmatched = build_evidence_rows(listings, nasdaq_records, nyse_records)

    output_dir.mkdir(parents=True, exist_ok=False)
    evidence_path = output_dir / "official_issue_level_evidence.csv"
    _write_evidence_csv(evidence_path, evidence)
    result = classifier.run_pipeline(
        database_path=database_path,
        evidence_path=evidence_path,
        output_dir=output_dir,
    )
    manifest = {
        "generated_at_utc": _utc_now().isoformat(),
        "database_access": "SQLite URI mode=ro",
        "listing_rows": len(listings),
        "nasdaq_source_records": len(nasdaq_records),
        "nyse_source_records": sum(len(source["rows"]) for source in nyse_source.values()),
        "evidence_rows_matched_by_ticker_and_exchange": len(evidence),
        "unmatched_source_records": unmatched,
        "sources": [nasdaq_metadata] + [
            nyse_source[category]["metadata"] for category in NYSE_CATEGORIES
        ],
        "cache_directory": str(cache_dir),
        "outputs": {
            "evidence_csv": str(evidence_path),
            "candidate_csv": str(result["candidate_csv"]),
            "manual_review_csv": str(result["manual_review_csv"]),
            "report": str(result["report"]),
        },
    }
    _write_json_new(output_dir / "source_manifest.json", manifest)
    result.update({
        "evidence_count": len(evidence),
        "unmatched_source_records": unmatched,
        "manifest": output_dir / "source_manifest.json",
    })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect official exchange listing evidence and classify candidates.")
    parser.add_argument("--database", type=Path, default=classifier.DATABASE_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--refresh", action="store_true", help="Fetch fresh official source snapshots.")
    args = parser.parse_args(argv)
    result = run_phase2(args.database, args.output_dir, args.cache_dir, args.refresh)
    print("US official issue-level evidence collection completed")
    print(f"Listings evaluated: {result['candidate_count']}")
    print(f"Matched official evidence rows: {result['evidence_count']}")
    print(f"Confirmed: {result['confirmed_count']}")
    print(f"Manual review required: {result['manual_review_count']}")
    print(f"Candidate CSV: {result['candidate_csv']}")
    print(f"Manual-review CSV: {result['manual_review_csv']}")
    print(f"Report: {result['report']}")
    print(f"Source manifest: {result['manifest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
