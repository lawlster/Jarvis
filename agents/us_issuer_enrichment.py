import argparse
import csv
import email.utils
import filecmp
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "database" / "assistant.db"
OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us"
CACHE_PATH = OUTPUT_DIR / "sec_issuer_enrichment_cache.json"
JOURNAL_DIR = OUTPUT_DIR / "sec_issuer_enrichment_journal"
CSV_PATH = OUTPUT_DIR / "sec_issuer_enrichment.csv"
REPORT_PATH = OUTPUT_DIR / "sec_issuer_enrichment_report.txt"
COVERAGE_PATH = OUTPUT_DIR / "sec_issuer_enrichment_coverage.json"

load_dotenv(BASE_DIR / ".env")

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{}.json"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
REQUEST_DELAY = 0.15
MAX_RETRIES = 3
DEFAULT_REFRESH_DAYS = 30
CIK_PATTERN = re.compile(r"^\d{10}$")

CACHE_METADATA_FIELDS = {
    "cik",
    "sec_company_name",
    "sic",
    "sic_description",
    "entity_type",
    "tickers",
    "exchanges",
    "fiscal_year_end",
    "state_of_incorporation",
    "state_of_incorporation_description",
    "former_names",
    "filing_history",
}


def get_us_issuers(read_only=False, database_path=None, allowlist=None):
    """Return every US listing grouped by CIK without deduplicating listings."""

    database_path = Path(database_path) if database_path else DATABASE_PATH
    if not database_path.exists():
        raise FileNotFoundError(f"Database not found: {database_path}")

    if read_only:
        database_uri = (
            "file:"
            + quote(str(database_path.resolve()), safe="/:")
            + "?mode=ro"
        )
        conn = sqlite3.connect(database_uri, uri=True)
    else:
        conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT ticker, company_name, exchange, sec_cik
            FROM market_universe
            WHERE country = 'USA'
              AND sec_cik IS NOT NULL
              AND TRIM(sec_cik) != ''
            ORDER BY sec_cik, ticker, exchange
        """).fetchall()
    finally:
        conn.close()

    issuers = {}
    for row in rows:
        raw_cik = str(row["sec_cik"]).strip()
        if not raw_cik.isdigit() or len(raw_cik) > 10:
            continue
        cik = raw_cik.zfill(10)
        issuer = issuers.setdefault(cik, {
            "cik": cik,
            "database_tickers": [],
            "database_company_names": [],
            "database_exchanges": [],
        })
        issuer["database_tickers"].append(row["ticker"])
        issuer["database_company_names"].append(row["company_name"])
        issuer["database_exchanges"].append(row["exchange"])

    if allowlist is not None:
        issuers = {cik: issuer for cik, issuer in issuers.items() if cik in allowlist}
    return issuers


def validate_pilot_ciks(values, database_path=None):
    if not values:
        raise ValueError("--pilot-ciks requires exactly 20 distinct CIKs.")
    normalized = []
    for value in values:
        raw = str(value).strip()
        if not raw.isdigit() or len(raw) > 10:
            raise ValueError(f"Invalid CIK: {value}")
        normalized.append(raw.zfill(10))
    if len(normalized) != 20:
        raise ValueError("Pilot mode requires exactly 20 CIKs.")
    if len(set(normalized)) != 20:
        raise ValueError("Pilot CIKs must be distinct.")
    found = get_us_issuers(read_only=True, database_path=database_path)
    unknown = sorted(set(normalized).difference(found))
    if unknown:
        raise ValueError("Pilot CIKs absent from production database: " + ", ".join(unknown))
    return set(normalized)


def pilot_paths(output_dir):
    root = Path(output_dir).expanduser().resolve()
    production = [OUTPUT_DIR.resolve(), CACHE_PATH.resolve(), JOURNAL_DIR.resolve(), CSV_PATH.resolve(), REPORT_PATH.resolve(), COVERAGE_PATH.resolve()]
    for path in production:
        if root == path or root in path.parents or path in root.parents:
            raise ValueError(f"Pilot output directory overlaps production path: {path}")
    return {
        "output": root,
        "cache": root / CACHE_PATH.name,
        "journal": root / JOURNAL_DIR.name,
        "csv": root / CSV_PATH.name,
        "report": root / REPORT_PATH.name,
        "coverage": root / COVERAGE_PATH.name,
    }


def get_refresh_days():
    """Read the metadata refresh interval from SEC_ISSUER_REFRESH_DAYS."""

    raw_value = os.getenv(
        "SEC_ISSUER_REFRESH_DAYS",
        str(DEFAULT_REFRESH_DAYS),
    ).strip()
    try:
        refresh_days = int(raw_value)
    except ValueError as error:
        raise ValueError(
            "SEC_ISSUER_REFRESH_DAYS must be a non-negative integer."
        ) from error
    if refresh_days < 0:
        raise ValueError(
            "SEC_ISSUER_REFRESH_DAYS must be a non-negative integer."
        )
    return refresh_days


def load_cache(path=None):
    """Parse the cache without modifying it; malformed JSON fails closed."""

    cache_path = Path(path) if path else CACHE_PATH
    if not cache_path.exists():
        return {}

    try:
        with cache_path.open("r", encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Issuer cache could not be read safely: {error}"
        ) from error

    if not isinstance(cache, dict):
        raise RuntimeError("Issuer cache root must be a JSON object.")
    return cache


def cache_record_error(cik, record):
    """Return a reason when a cache entry is malformed, otherwise None."""

    if not isinstance(cik, str) or not CIK_PATTERN.fullmatch(cik):
        return "cache key is not a ten-digit CIK"
    if not isinstance(record, dict):
        return "issuer record is not a JSON object"
    if not isinstance(record.get("cik"), str):
        return "record CIK is missing or not text"
    if record["cik"].strip().zfill(10) != cik:
        return "record CIK does not match its cache key"
    missing_fields = CACHE_METADATA_FIELDS.difference(record)
    if missing_fields:
        return "missing fields: " + ", ".join(sorted(missing_fields))
    if not isinstance(record.get("tickers"), list):
        return "SEC tickers field is not a list"
    if not isinstance(record.get("exchanges"), list):
        return "SEC exchanges field is not a list"
    if not isinstance(record.get("former_names"), list):
        return "former names field is not a list"
    filing_history = record.get("filing_history")
    if (
        not isinstance(filing_history, dict)
        or not isinstance(filing_history.get("recent"), dict)
        or not isinstance(filing_history.get("files"), list)
    ):
        return "filing history has an unexpected structure"
    for field in (
        "database_tickers",
        "database_company_names",
        "database_exchanges",
    ):
        if field in record and not isinstance(record[field], list):
            return f"{field} is not a list"
    return None


def get_invalid_cache_entries(cache):
    return {
        cik: cache_record_error(cik, record)
        for cik, record in cache.items()
        if cache_record_error(cik, record) is not None
    }


def parse_retrieved_at(record):
    value = record.get("retrieved_at_utc")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_cache_record_stale(record, refresh_days, now=None):
    retrieved_at = parse_retrieved_at(record)
    if retrieved_at is None:
        return True
    current_time = now or datetime.now(timezone.utc)
    return retrieved_at < current_time - timedelta(days=refresh_days)


def update_database_listing_metadata(record, database_record):
    """Refresh listing mappings while retaining issuer-level SEC fields."""

    record["database_tickers"] = list(database_record["database_tickers"])
    record["database_company_names"] = list(
        database_record["database_company_names"]
    )
    record["database_exchanges"] = list(database_record["database_exchanges"])


def extract_issuer_metadata(cik, data, retrieved_at=None):
    filings = data.get("filings") or {}
    if not isinstance(filings, dict):
        filings = {}
    recent = filings.get("recent") or {}
    files = filings.get("files") or []

    return {
        "cik": cik,
        "retrieved_at_utc": retrieved_at or datetime.now(timezone.utc).isoformat(),
        "sec_company_name": data.get("name"),
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "entity_type": data.get("entityType"),
        "tickers": data.get("tickers") if isinstance(data.get("tickers"), list) else [],
        "exchanges": data.get("exchanges") if isinstance(data.get("exchanges"), list) else [],
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "state_of_incorporation": data.get("stateOfIncorporation"),
        "state_of_incorporation_description": data.get(
            "stateOfIncorporationDescription"
        ),
        "former_names": data.get("formerNames") if isinstance(data.get("formerNames"), list) else [],
        "filing_history": {
            "recent": recent if isinstance(recent, dict) else {},
            "files": files if isinstance(files, list) else [],
        },
    }


def retry_delay(response, attempt, now=None):
    """Return SEC Retry-After seconds, or exponential backoff if absent/bad."""

    fallback = float(attempt * 2)
    if response is None:
        return fallback
    value = response.headers.get("Retry-After")
    if value is None:
        return fallback
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            current_time = now or datetime.now(timezone.utc)
            return max(0.0, (retry_at - current_time).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback


def fetch_sec_submission(cik, session, sleep=time.sleep):
    url = SEC_SUBMISSIONS_URL.format(cik)

    for attempt in range(1, MAX_RETRIES + 1):
        response = None
        try:
            response = session.get(url, timeout=30)
        except requests.RequestException as error:
            if attempt == MAX_RETRIES:
                return {"success": False, "error": str(error)}
            sleep(float(attempt * 2))
            continue

        status = response.status_code
        if status == 404:
            return {"success": False, "error": "SEC record not found (HTTP 404)"}
        if status == 429 or 500 <= status <= 599:
            if attempt == MAX_RETRIES:
                return {
                    "success": False,
                    "error": f"SEC request failed after {attempt} attempts (HTTP {status})",
                }
            sleep(retry_delay(response, attempt))
            continue

        try:
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            return {"success": False, "error": str(error)}
        except ValueError as error:
            return {"success": False, "error": f"Invalid JSON: {error}"}

        if not isinstance(data, dict):
            return {"success": False, "error": "Unexpected SEC response format"}
        return {"success": True, "data": data}

    return {"success": False, "error": "Unknown retrieval error"}


def journal_record_path(cik, journal_dir=None):
    directory = Path(journal_dir) if journal_dir else JOURNAL_DIR
    return directory / f"{cik}.json"


def load_journal_records(journal_dir=None):
    """Replay valid per-CIK checkpoints; leave malformed files untouched."""

    directory = Path(journal_dir) if journal_dir else JOURNAL_DIR
    records = {}
    invalid_files = []
    if not directory.exists():
        return records, invalid_files

    for path in sorted(directory.glob("*.json")):
        cik = path.stem
        try:
            with path.open("r", encoding="utf-8") as journal_file:
                record = json.load(journal_file)
        except (OSError, json.JSONDecodeError):
            invalid_files.append(path.name)
            continue
        if cache_record_error(cik, record):
            invalid_files.append(path.name)
            continue
        records[cik] = record
    return records, invalid_files


def save_journal_record(cik, record, journal_dir=None):
    """Atomically checkpoint one issuer so interrupted runs can resume."""

    directory = Path(journal_dir) if journal_dir else JOURNAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = journal_record_path(cik, directory)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as journal_file:
        json.dump(record, journal_file, ensure_ascii=False)
        journal_file.flush()
        os.fsync(journal_file.fileno())
    if target.exists():
        archive_dir = directory / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(target, archive_dir / f"{cik}-{stamp}.json")
    os.replace(temporary, target)


def ensure_cache_update_capacity(cache_path=None):
    """Check staging and backup space before starting expensive SEC requests."""

    target = Path(cache_path) if cache_path else CACHE_PATH
    if not target.exists():
        return
    required_space = get_cache_update_space_requirement(target)
    free_space = shutil.disk_usage(target.parent).free
    if free_space < required_space:
        raise OSError(
            "Not enough free disk space to stage the cache and create "
            "a verified recovery backup. No SEC requests were started."
        )


def get_cache_update_space_requirement(cache_path=None):
    """Return the staging and recovery reserve used by cache replacement."""

    target = Path(cache_path) if cache_path else CACHE_PATH
    if not target.exists():
        return 64 * 1024 * 1024
    return target.stat().st_size * 3 + 64 * 1024 * 1024


def save_cache(cache, cache_path=None):
    """Validate staged JSON and keep a verified recovery copy before replace."""

    target = Path(cache_path) if cache_path else CACHE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    temporary = target.with_name(f"{target.name}.{stamp}.tmp")

    if target.exists():
        ensure_cache_update_capacity(target)

    with temporary.open("w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file, indent=2, ensure_ascii=False)
        cache_file.flush()
        os.fsync(cache_file.fileno())

    staged_cache = load_cache(temporary)
    if staged_cache != cache:
        raise RuntimeError("Staged cache did not match the in-memory cache.")

    backup_path = None
    if target.exists():
        backup_path = target.with_name(
            f"{target.stem}.backup-{stamp}{target.suffix}"
        )
        shutil.copy2(target, backup_path)
        if not filecmp.cmp(target, backup_path, shallow=False):
            raise RuntimeError(
                f"Cache backup verification failed; original retained at {target}."
            )

    os.replace(temporary, target)
    return backup_path


def build_coverage(issuers, cache, refresh_days, now=None):
    current_time = now or datetime.now(timezone.utc)
    invalid_cache = get_invalid_cache_entries(cache)
    coverage = {
        "generated_at_utc": current_time.isoformat(),
        "refresh_days": refresh_days,
        "database_ticker_records": sum(
            len(record["database_tickers"]) for record in issuers.values()
        ),
        "database_unique_ciks": len(issuers),
        "cache_record_count": len(cache),
        "extra_cache_ciks": sorted(set(cache).difference(issuers)),
        "matched_ciks": [],
        "missing_ciks": [],
        "invalid_ciks": [],
        "stale_ciks": [],
    }

    for cik in sorted(issuers):
        record = cache.get(cik)
        if record is None:
            coverage["missing_ciks"].append(cik)
        elif cache_record_error(cik, record):
            coverage["invalid_ciks"].append(cik)
        elif is_cache_record_stale(record, refresh_days, current_time):
            coverage["stale_ciks"].append(cik)
        else:
            coverage["matched_ciks"].append(cik)

    coverage["invalid_cache_keys"] = sorted(invalid_cache)
    coverage["extra_cache_cik_count"] = len(coverage["extra_cache_ciks"])
    for status in ("matched", "missing", "invalid", "stale"):
        coverage[f"{status}_count"] = len(coverage[f"{status}_ciks"])
    return coverage


def run_preflight():
    """Report cache coverage and disk capacity without writing or networking."""

    try:
        issuers = get_us_issuers(read_only=True)
    except Exception as error:
        print(f"Preflight failed: unable to read production database read-only: {error}")
        print("Controlled enrichment can proceed: no")
        return 2

    try:
        cache = load_cache()
    except (RuntimeError, OSError) as error:
        print(f"Cache JSON valid: no ({error})")
        print("Controlled enrichment can proceed: no")
        return 2

    refresh_days = get_refresh_days()
    invalid_entries = get_invalid_cache_entries(cache)
    coverage = build_coverage(issuers, cache, refresh_days)
    cache_size = CACHE_PATH.stat().st_size if CACHE_PATH.exists() else 0
    required_space = get_cache_update_space_requirement()
    free_space = shutil.disk_usage(CACHE_PATH.parent).free
    disk_capacity_ok = free_space >= required_space
    cache_records_valid = not invalid_entries
    can_proceed = cache_records_valid and disk_capacity_ok

    print("US SEC issuer enrichment cache preflight")
    print("SEC requests made: 0")
    print("Production files modified: no")
    print(f"Production database opened read-only: yes")
    print(f"Database ticker records: {coverage['database_ticker_records']}")
    print(f"Database unique CIKs: {coverage['database_unique_ciks']}")
    print(f"Cache file present: {'yes' if CACHE_PATH.exists() else 'no'}")
    print(f"Cache file bytes: {cache_size}")
    print(f"Cache JSON valid: yes")
    print(f"Valid cache records: {len(cache) - len(invalid_entries)}")
    print(f"Invalid cache records: {len(invalid_entries)}")
    print(f"Fresh current CIKs: {coverage['matched_count']}")
    print(f"Stale current CIKs: {coverage['stale_count']}")
    print(f"Missing current CIKs: {coverage['missing_count']}")
    print(f"Invalid current CIKs: {coverage['invalid_count']}")
    print(f"Extra cached CIKs: {coverage['extra_cache_cik_count']}")
    print(f"Refresh policy days: {refresh_days}")
    print(f"Available disk bytes: {free_space}")
    print(f"Required disk bytes: {required_space}")
    print(f"Disk capacity sufficient: {'yes' if disk_capacity_ok else 'no'}")
    print(
        "Controlled enrichment can proceed: "
        f"{'yes' if can_proceed else 'no'}"
    )
    return 0 if can_proceed else 1


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    temporary = path.with_name(f"{path.name}.{stamp}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output_file:
        output_file.write(text)
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary, path)


def export_csv(issuers, cache, path=None):
    output_path = Path(path) if path else CSV_PATH
    fieldnames = [
        "cik",
        "retrieved_at_utc",
        "sec_company_name",
        "sic",
        "sic_description",
        "entity_type",
        "tickers",
        "exchanges",
        "fiscal_year_end",
        "state_of_incorporation",
        "state_of_incorporation_description",
        "former_names",
        "filing_history",
        "database_tickers",
        "database_company_names",
        "database_exchanges",
    ]
    rows = []
    for cik in sorted(issuers):
        record = cache.get(cik)
        if cache_record_error(cik, record):
            continue
        row = dict(record)
        update_database_listing_metadata(row, issuers[cik])
        for field in (
            "tickers",
            "exchanges",
            "former_names",
            "filing_history",
            "database_tickers",
            "database_company_names",
            "database_exchanges",
        ):
            row[field] = json.dumps(row.get(field, []), ensure_ascii=False)
        rows.append({field: row.get(field) for field in fieldnames})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    temporary = output_path.with_name(f"{output_path.name}.{stamp}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_file.flush()
        os.fsync(csv_file.fileno())
    os.replace(temporary, output_path)
    return len(rows)


def build_report(issuers, cache, coverage, failures, invalid_journal_files):
    lines = [
        "AiAssistant - US Issuer Enrichment Report",
        "=" * 50,
        f"Generated at UTC: {coverage['generated_at_utc']}",
        f"Refresh policy (days): {coverage['refresh_days']}",
        f"Database ticker records: {coverage['database_ticker_records']}",
        f"Database unique CIKs: {coverage['database_unique_ciks']}",
        f"Cache issuer records: {coverage['cache_record_count']}",
        f"Cached CIKs absent from database: {coverage['extra_cache_cik_count']}",
        f"Matched CIKs: {coverage['matched_count']}",
        f"Missing CIKs: {coverage['missing_count']}",
        f"Invalid CIK records: {coverage['invalid_count']}",
        f"Stale CIKs: {coverage['stale_count']}",
        f"Invalid cache keys (including non-current CIKs): {len(coverage['invalid_cache_keys'])}",
        f"Failed SEC requests this run: {len(failures)}",
        f"Invalid journal files: {len(invalid_journal_files)}",
        "",
        "SIC INDUSTRY DESCRIPTIONS (CURRENT VALID CACHE RECORDS)",
        "-" * 50,
    ]

    sic_counts = {}
    for cik in issuers:
        record = cache.get(cik)
        if cache_record_error(cik, record):
            continue
        label = record.get("sic_description") or "Unknown"
        sic_counts[label] = sic_counts.get(label, 0) + 1
    for description, count in sorted(
        sic_counts.items(), key=lambda item: item[1], reverse=True
    )[:40]:
        lines.append(f"{description}: {count}")

    for label, key in (
        ("MATCHED CIKS", "matched_ciks"),
        ("MISSING CIKS", "missing_ciks"),
        ("INVALID CIKS", "invalid_ciks"),
        ("STALE CIKS", "stale_ciks"),
    ):
        lines.extend(["", label, "-" * 50])
        lines.extend(coverage[key] or ["None"])

    lines.extend(["", "CACHE CIKs ABSENT FROM DATABASE", "-" * 50])
    lines.extend(coverage["extra_cache_ciks"] or ["None"])

    lines.extend(["", "FAILED REQUESTS", "-" * 50])
    lines.extend(
        f"{item['cik']}: {item['error']}" for item in failures
    )
    if not failures:
        lines.append("None")

    lines.extend(["", "INVALID JOURNAL FILES", "-" * 50])
    lines.extend(invalid_journal_files or ["None"])
    return "\n".join(lines) + "\n"


def run_enrichment(pilot_ciks=None, pilot_output_dir=None):
    if not SEC_USER_AGENT:
        raise RuntimeError(
            "Configure SEC_USER_AGENT in .env before contacting SEC EDGAR."
        )

    pilot = pilot_ciks is not None
    paths = pilot_paths(pilot_output_dir) if pilot else {
        "output": OUTPUT_DIR, "cache": CACHE_PATH, "journal": JOURNAL_DIR,
        "csv": CSV_PATH, "report": REPORT_PATH, "coverage": COVERAGE_PATH,
    }
    issuers = get_us_issuers(read_only=pilot, allowlist=pilot_ciks)
    if pilot and (len(pilot_ciks) != 20 or len(issuers) != 20):
        raise RuntimeError("Pilot issuer selection did not resolve to exactly 20 CIKs.")
    print(f"Selected CIK count: {len(issuers)}")
    print(f"Planned cache: {paths['cache']}")
    print(f"Planned journal: {paths['journal']}")
    print(f"Planned CSV: {paths['csv']}")
    print(f"Planned report: {paths['report']}")
    print(f"Planned coverage: {paths['coverage']}")
    paths["output"].mkdir(parents=True, exist_ok=True)
    refresh_days = get_refresh_days()
    cache = load_cache(paths["cache"])
    if pilot:
        cache = {cik: record for cik, record in cache.items() if cik in pilot_ciks}
    invalid_entries = get_invalid_cache_entries(cache)
    journal_records, invalid_journal_files = load_journal_records(paths["journal"])
    if pilot:
        journal_records = {cik: record for cik, record in journal_records.items() if cik in pilot_ciks}
        invalid_journal_files = [name for name in invalid_journal_files if Path(name).stem in pilot_ciks]
    changed = False

    # Replay completed requests from an interrupted prior run before fetching.
    for cik, record in journal_records.items():
        cached = cache.get(cik)
        if (
            cached is None
            or cache_record_error(cik, cached)
            or (parse_retrieved_at(record) or datetime.min.replace(tzinfo=timezone.utc))
            > (parse_retrieved_at(cached) or datetime.min.replace(tzinfo=timezone.utc))
        ):
            cache[cik] = record
            changed = True

    for cik, database_record in issuers.items():
        record = cache.get(cik)
        if record is not None and not cache_record_error(cik, record):
            old_mapping = (
                record.get("database_tickers", []),
                record.get("database_company_names", []),
                record.get("database_exchanges", []),
            )
            update_database_listing_metadata(record, database_record)
            new_mapping = (
                record["database_tickers"],
                record["database_company_names"],
                record["database_exchanges"],
            )
            if old_mapping != new_mapping:
                changed = True

    cache_needs_refresh = any(
        cik not in cache
        or cache_record_error(cik, cache[cik])
        or is_cache_record_stale(cache[cik], refresh_days)
        for cik in issuers
    )
    if changed or cache_needs_refresh:
        ensure_cache_update_capacity(paths["cache"])

    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    session = requests.Session()
    session.headers.update(headers)
    failures = []
    try:
        for cik, database_record in issuers.items():
            cached = cache.get(cik)
            invalid = cache_record_error(cik, cached) if cached is not None else None
            if (
                cached is not None
                and invalid is None
                and not is_cache_record_stale(cached, refresh_days)
            ):
                continue

            result = fetch_sec_submission(cik, session)
            time.sleep(REQUEST_DELAY)
            if not result["success"]:
                failures.append({"cik": cik, "error": result["error"]})
                continue

            metadata = extract_issuer_metadata(cik, result["data"])
            update_database_listing_metadata(metadata, database_record)
            save_journal_record(cik, metadata, paths["journal"])
            cache[cik] = metadata
            changed = True
    finally:
        session.close()

    invalid_entries = get_invalid_cache_entries(cache)
    backup_path = save_cache(cache, paths["cache"]) if changed else None

    coverage = build_coverage(issuers, cache, refresh_days)
    coverage["invalid_journal_files"] = invalid_journal_files
    coverage["failed_request_ciks"] = [item["cik"] for item in failures]
    coverage["invalid_cache_details"] = invalid_entries
    csv_rows = export_csv(issuers, cache, paths["csv"])
    coverage["csv_rows"] = csv_rows
    _atomic_write_text(
        paths["coverage"],
        json.dumps(coverage, indent=2, ensure_ascii=False) + "\n",
    )
    _atomic_write_text(
        paths["report"],
        build_report(issuers, cache, coverage, failures, invalid_journal_files),
    )

    print("US SEC issuer enrichment complete")
    print(f"Database ticker records: {coverage['database_ticker_records']}")
    print(f"Database unique CIKs: {coverage['database_unique_ciks']}")
    print(
        "Coverage: "
        f"matched={coverage['matched_count']}, "
        f"missing={coverage['missing_count']}, "
        f"invalid={coverage['invalid_count']}, "
        f"stale={coverage['stale_count']}"
    )
    print(f"CSV rows: {csv_rows}")
    print(f"Cache rewritten: {'yes' if changed else 'no'}")
    if backup_path:
        print(f"Verified recovery backup: {backup_path}")
    print(f"CSV: {paths['csv']}")
    print(f"Report: {paths['report']}")
    print(f"Coverage: {paths['coverage']}")
    print("Database modified: no")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Enrich US SEC issuer metadata."
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "Validate cache coverage and disk space without making SEC "
            "requests or writing files."
        ),
    )
    parser.add_argument("--pilot-ciks", nargs="+", help="Exactly 20 production CIKs for isolated pilot mode.")
    parser.add_argument("--pilot-output-dir", help="Isolated output directory for pilot cache, journal, and reports.")
    args = parser.parse_args(argv)

    if bool(args.pilot_ciks) != bool(args.pilot_output_dir):
        parser.error("--pilot-ciks and --pilot-output-dir must be used together.")
    if args.pilot_ciks:
        if args.preflight_only:
            parser.error("--preflight-only cannot be combined with pilot options.")
        try:
            pilot = validate_pilot_ciks(args.pilot_ciks)
            pilot_paths(args.pilot_output_dir)
        except (ValueError, OSError) as error:
            parser.error(str(error))
    else:
        pilot = None

    if args.preflight_only:
        return run_preflight()

    run_enrichment(pilot, args.pilot_output_dir if pilot is not None else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
