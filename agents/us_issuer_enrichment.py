
import csv
import json
import time
import sqlite3
from pathlib import Path

import requests


# ============================================================
# PATHS AND SETTINGS
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]

DATABASE_PATH = BASE_DIR / "database" / "assistant.db"

OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "market_data"
    / "us"
)

CACHE_PATH = OUTPUT_DIR / "sec_issuer_enrichment_cache.json"
CSV_PATH = OUTPUT_DIR / "sec_issuer_enrichment.csv"
REPORT_PATH = OUTPUT_DIR / "sec_issuer_enrichment_report.txt"

SEC_SUBMISSIONS_URL = (
    "https://data.sec.gov/submissions/CIK{}.json"
)

SEC_USER_AGENT = (
    "AiAssistant Research checkme123ymail.com@gmail.com"
)

REQUEST_DELAY = 0.15
MAX_RETRIES = 3
SAVE_INTERVAL = 25


# ============================================================
# DATABASE
# ============================================================

def get_us_issuers():

    if not DATABASE_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DATABASE_PATH}"
        )

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT
                ticker,
                company_name,
                exchange,
                sec_cik
            FROM market_universe
            WHERE country = 'USA'
              AND sec_cik IS NOT NULL
              AND TRIM(sec_cik) != ''
            ORDER BY sec_cik, ticker
        """).fetchall()

    finally:
        conn.close()

    issuers = {}

    for row in rows:

        cik = str(row["sec_cik"]).strip().zfill(10)

        if cik not in issuers:
            issuers[cik] = {
                "cik": cik,
                "database_tickers": [],
                "database_company_names": [],
                "database_exchanges": []
            }

        issuers[cik]["database_tickers"].append(
            row["ticker"]
        )

        issuers[cik]["database_company_names"].append(
            row["company_name"]
        )

        issuers[cik]["database_exchanges"].append(
            row["exchange"]
        )

    return issuers


# ============================================================
# CACHE
# ============================================================

def load_cache():

    if not CACHE_PATH.exists():
        return {}

    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as file:
            cache = json.load(file)

        if isinstance(cache, dict):
            return cache

    except (json.JSONDecodeError, OSError):
        print("Warning: Cache could not be loaded.")

    return {}


def save_cache(cache):

    temporary_path = CACHE_PATH.with_suffix(".tmp")

    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(
            cache,
            file,
            indent=2,
            ensure_ascii=False
        )

    temporary_path.replace(CACHE_PATH)


# ============================================================
# SEC API
# ============================================================

def fetch_sec_submission(cik, session):

    url = SEC_SUBMISSIONS_URL.format(cik)

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.get(
                url,
                timeout=30
            )

            if response.status_code == 404:
                return {
                    "success": False,
                    "error": "SEC record not found"
                }

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                return {
                    "success": False,
                    "error": "Unexpected SEC response format"
                }

            return {
                "success": True,
                "data": data
            }

        except requests.RequestException as error:

            if attempt == MAX_RETRIES:
                return {
                    "success": False,
                    "error": str(error)
                }

            time.sleep(attempt * 2)

        except ValueError as error:

            return {
                "success": False,
                "error": f"Invalid JSON: {error}"
            }

    return {
        "success": False,
        "error": "Unknown retrieval error"
    }


# ============================================================
# NORMALISE SEC ISSUER METADATA
# ============================================================

def extract_issuer_metadata(cik, data):

    return {
        "cik": cik,
        "sec_company_name": data.get("name"),
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "entity_type": data.get("entityType"),
        "tickers": data.get("tickers", []),
        "exchanges": data.get("exchanges", []),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "state_of_incorporation": data.get(
            "stateOfIncorporation"
        ),
        "state_of_incorporation_description": data.get(
            "stateOfIncorporationDescription"
        ),
        "former_names": data.get("formerNames", []),
        "filing_history": {
            "recent": data.get("filings", {}).get(
                "recent", {}
            ),
            "files": data.get("filings", {}).get(
                "files", []
            )
        }
    }


# ============================================================
# ENRICHMENT
# ============================================================

def run_enrichment():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    print("=" * 60)
    print("AiAssistant - US Issuer Enrichment")
    print("=" * 60)

    issuers = get_us_issuers()

    print("Database ticker records:", sum(
        len(item["database_tickers"])
        for item in issuers.values()
    ))

    print("Unique CIKs:", len(issuers))

    cache = load_cache()

    print("Cached issuer records:", len(cache))

    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate"
    }

    session = requests.Session()
    session.headers.update(headers)

    successful = 0
    failed = 0
    processed = 0

    failures = []

    for cik, database_record in issuers.items():

        if cik in cache:
            continue

        result = fetch_sec_submission(
            cik,
            session
        )

        processed += 1

        if result["success"]:

            metadata = extract_issuer_metadata(
                cik,
                result["data"]
            )

            metadata["database_tickers"] = (
                database_record["database_tickers"]
            )

            metadata["database_company_names"] = (
                database_record["database_company_names"]
            )

            metadata["database_exchanges"] = (
                database_record["database_exchanges"]
            )

            cache[cik] = metadata
            successful += 1

        else:

            failures.append({
                "cik": cik,
                "error": result["error"]
            })

            failed += 1

        if processed % 25 == 0:

            save_cache(cache)

            print(
                f"Processed {processed} new CIKs | "
                f"Successful: {successful} | "
                f"Failed: {failed}"
            )

        time.sleep(REQUEST_DELAY)

    save_cache(cache)

    session.close()

    # --------------------------------------------------------
    # EXPORT CSV
    # --------------------------------------------------------

    fieldnames = [
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
        "database_tickers",
        "database_exchanges"
    ]

    with open(
        CSV_PATH,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames
        )

        writer.writeheader()

        for record in cache.values():

            row = dict(record)

            for field in [
                "tickers",
                "exchanges",
                "former_names",
                "database_tickers",
                "database_exchanges"
            ]:

                row[field] = json.dumps(
                    row.get(field, []),
                    ensure_ascii=False
                )

            writer.writerow({
                field: row.get(field)
                for field in fieldnames
            })

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    sic_counts = {}

    for record in cache.values():

        sic = record.get("sic_description") or "Unknown"

        sic_counts[sic] = sic_counts.get(sic, 0) + 1

    report = [
        "AiAssistant - US Issuer Enrichment Report",
        "=" * 50,
        f"Database unique CIKs: {len(issuers)}",
        f"Cached SEC issuer records: {len(cache)}",
        f"Successfully retrieved this run: {successful}",
        f"Failed this run: {failed}",
        f"Remaining uncached CIKs: {max(0, len(issuers) - len(cache))}",
        "",
        "TOP SIC INDUSTRY DESCRIPTIONS",
        "-" * 50
    ]

    for description, count in sorted(
        sic_counts.items(),
        key=lambda item: item[1],
        reverse=True
    )[:40]:

        report.append(
            f"{description}: {count}"
        )

    report.extend([
        "",
        "FAILED REQUESTS THIS RUN",
        "-" * 50
    ])

    for failure in failures:
        report.append(
            f"{failure['cik']}: {failure['error']}"
        )

    with open(
        REPORT_PATH,
        "w",
        encoding="utf-8"
    ) as file:

        file.write("\n".join(report))

    print("\n" + "=" * 60)
    print("ENRICHMENT COMPLETE")
    print("=" * 60)

    print("Successful this run:", successful)
    print("Failed this run:", failed)
    print("Total cached:", len(cache))

    print("\nGenerated files:")
    print(CACHE_PATH)
    print(CSV_PATH)
    print(REPORT_PATH)

    print("\nDatabase modified: NO")


if __name__ == "__main__":
    run_enrichment()
