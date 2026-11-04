
import json
import re
import time
import logging

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin
from html.parser import HTMLParser

import requests
import pymupdf

from data.financial_statements.statement_database import (
    initialise_statement_database,
    save_financial_statement,
    get_connection
)


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parents[2]

REPORT_DIR = (
    BASE_DIR
    / "data"
    / "financial_statements"
    / "sgx_reports"
)

SGX_API = "https://api.sgx.com/financialreports/v1.0"

PAGE_SIZE = 250
SOURCE_NAME = "SGX Announcements"

HEADERS = {
    "User-Agent": "AiAssistant Financial Research Collector"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ==================================================
# PDF LINK PARSER
# ==================================================

class PDFLinkParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):

        if tag.lower() != "a":
            return

        attributes = dict(attrs)
        href = attributes.get("href")

        if href and ".pdf" in href.lower():
            self.links.append(href)


# ==================================================
# DATABASE HELPERS
# ==================================================

def get_sgx_company(ticker):

    with get_connection() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM market_universe
            WHERE UPPER(ticker) = ?
              AND exchange = 'SGX'
            LIMIT 1
            """,
            (ticker.upper().strip(),)
        ).fetchone()

    return dict(row) if row else None


def record_collection_history(
    ticker,
    status,
    statements_collected=0,
    error_message=None,
    started_at=None
):

    completed_at = datetime.now(
        timezone.utc
    ).isoformat()

    if not started_at:
        started_at = completed_at

    with get_connection() as conn:

        conn.execute(
            """
            INSERT INTO statement_collection_history (
                ticker,
                exchange,
                source,
                status,
                statements_collected,
                error_message,
                started_at,
                completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker.upper(),
                "SGX",
                SOURCE_NAME,
                status,
                statements_collected,
                error_message,
                started_at,
                completed_at
            )
        )


# ==================================================
# SGX ANNOUNCEMENT DISCOVERY
# ==================================================

def get_sgx_announcements(max_pages=60):

    announcements = []

    for page in range(1, max_pages + 1):

        params = {
            "pagestart": page,
            "pagesize": PAGE_SIZE
        }

        try:

            response = SESSION.get(
                SGX_API,
                params=params,
                timeout=30
            )

            response.raise_for_status()

            payload = response.json()
            records = payload.get("data") or []

            if not records:

                logger.info(
                    "No more SGX records at page %s.",
                    page
                )

                break

            announcements.extend(records)

            logger.info(
                "Retrieved SGX page %s: %s records",
                page,
                len(records)
            )

            time.sleep(0.2)

        except Exception as error:

            raise RuntimeError(
                f"SGX announcement retrieval failed "
                f"on page {page}: {error}"
            ) from error

    return announcements


# ==================================================
# COMPANY MATCHING
# ==================================================

def normalise_company_name(name):

    if not name:
        return ""

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(name).upper()
    )


def match_company_announcements(
    announcements,
    company_name
):

    target = normalise_company_name(company_name)
    matches = []

    for item in announcements:

        names = [
            item.get("securityName", ""),
            item.get("companyName", "")
        ]

        for name in names:

            candidate = normalise_company_name(name)

            if candidate and candidate == target:

                matches.append(item)
                break

    return matches


# ==================================================
# REPORT FILTERING
# ==================================================

REPORT_KEYWORDS = [
    "annual report",
    "half year",
    "half-year",
    "interim financial",
    "financial statements",
    "full year results",
    "half year results",
    "quarterly results",
    "financial results"
]

EXCLUDED_KEYWORDS = [
    "sustainability report",
    "notice of annual general meeting",
    "notice of extraordinary general meeting",
    "circular",
    "letter to shareholders",
    "share purchase mandate",
    "proxy form",
    "presentation slides"
]


def is_financial_report(item):

    title = str(
        item.get("title", "")
    ).lower().strip()

    if any(
        keyword in title
        for keyword in EXCLUDED_KEYWORDS
    ):
        return False

    return any(
        keyword in title
        for keyword in REPORT_KEYWORDS
    )


# ==================================================
# PDF DOWNLOAD
# ==================================================

def get_pdf_links(announcement_url):

    response = SESSION.get(
        announcement_url,
        timeout=30
    )

    response.raise_for_status()

    parser = PDFLinkParser()
    parser.feed(response.text)

    links = []

    for link in parser.links:

        absolute_url = urljoin(
            announcement_url,
            link
        )

        if absolute_url not in links:
            links.append(absolute_url)

    return links


def download_pdf(url, destination):

    destination = Path(destination)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    if destination.exists():

        if destination.stat().st_size > 0:

            with destination.open("rb") as file:

                if file.read(5) == b"%PDF-":
                    return destination

    response = SESSION.get(
        url,
        timeout=90
    )

    response.raise_for_status()

    if not response.content.startswith(b"%PDF-"):

        raise ValueError(
            "Downloaded document is not a valid PDF."
        )

    destination.write_bytes(
        response.content
    )

    return destination


# ==================================================
# STATEMENT PAGE IDENTIFICATION
# ==================================================

STATEMENT_HEADINGS = {

    "income_statement": [
        "consolidated income statement",
        "consolidated statement of profit or loss",
        "consolidated statement of comprehensive income",
        "consolidated statement of profit or loss and other comprehensive income",
        "income statement"
    ],

    "balance_sheet": [
        "consolidated balance sheet",
        "consolidated balance sheets",
        "consolidated statement of financial position",
        "balance sheet"
    ],

    "cash_flow": [
        "consolidated cash flow statement",
        "consolidated statement of cash flows",
        "consolidated cash flows",
        "cash flow statement"
    ],

    "changes_in_equity": [
        "consolidated statement of changes in equity",
        "statement of changes in equity"
    ]

}


AUDITOR_REPORT_MARKERS = [
    "independent auditor's report",
    "independent auditors' report",
    "report on the audit of the financial statements",
    "basis for opinion",
    "auditor's responsibilities for the audit"
]


REJECTED_PAGE_MARKERS = [
    "additional guidelines",
    "express disclosure requirements",
    "page reference in",
    "guideline 1.17",
    "guideline 4.7",
    "principles and provisions of the 2018 code",
    "table of contents",
    "contents"
]


def normalise_page_text(text):

    text = str(text or "").lower()

    replacements = {
        "\u2019": "'",
        "\u2018": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\ufb01": "fi",
        "\ufb02": "fl"
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def clean_heading_line(line):

    line = normalise_page_text(line)

    line = re.sub(
        r"^\d+[.)]\s*",
        "",
        line
    )

    return line.strip(" :.-")


def is_auditor_report(page_text):

    text = normalise_page_text(page_text)

    # Auditor reports must be rejected even when they
    # mention the names of the actual financial statements.
    opening_text = text[:1800]

    return any(
        marker in opening_text
        for marker in AUDITOR_REPORT_MARKERS
    )


def is_rejected_page(page_text):

    text = normalise_page_text(page_text)

    if is_auditor_report(text):
        return True

    opening_text = text[:1800]

    return any(
        marker in opening_text
        for marker in REJECTED_PAGE_MARKERS
    )


def heading_found(page_text, headings):

    text = normalise_page_text(page_text)

    # Only inspect the beginning of the page.
    # A heading mentioned later in an article or note
    # does not qualify as a statement heading.
    opening_text = text[:1800]

    for heading in headings:

        pattern = (
            r"(?<![a-z])"
            + re.escape(heading)
            + r"(?![a-z])"
        )

        if re.search(pattern, opening_text):
            return True

    return False


# ==================================================
# STATEMENT-SPECIFIC VALIDATION
# ==================================================

STATEMENT_INDICATORS = {

    "income_statement": [
        "total income",
        "net interest income",
        "non-interest income",
        "total expenses",
        "profit before tax",
        "profit before taxation",
        "net profit",
        "profit for the year",
        "interest income",
        "revenue"
    ],

    "balance_sheet": [
        "total assets",
        "total liabilities",
        "total equity",
        "total shareholders' equity",
        "shareholders' funds",
        "equity attributable to shareholders",
        "deposits and balances from customers"
    ],

    "cash_flow": [
        "operating activities",
        "investing activities",
        "financing activities",
        "net cash generated from operating activities",
        "net cash used in investing activities",
        "net cash generated from financing activities",
        "cash flows from operating activities",
        "cash flows from investing activities",
        "cash flows from financing activities"
    ],

    "changes_in_equity": [
        "share capital",
        "retained profits",
        "revenue reserves",
        "other reserves",
        "total equity",
        "non-controlling interests",
        "balance at 1 january",
        "balance at 31 december"
    ]

}


def count_statement_indicators(page_text, statement_type):

    text = normalise_page_text(page_text)

    indicators = STATEMENT_INDICATORS.get(
        statement_type,
        []
    )

    return sum(
        1
        for indicator in indicators
        if indicator in text
    )


def validate_statement_page(page_text, statement_type):

    if not page_text:
        return False, "empty_page"

    if is_rejected_page(page_text):
        return False, "rejected_section"

    headings = STATEMENT_HEADINGS.get(
        statement_type,
        []
    )

    if not heading_found(page_text, headings):
        return False, "statement_heading_missing"

    indicator_count = count_statement_indicators(
        page_text,
        statement_type
    )

    # Require stronger evidence than a heading alone.
    minimum_indicators = {
        "income_statement": 3,
        "balance_sheet": 3,
        "cash_flow": 3,
        "changes_in_equity": 3
    }

    required = minimum_indicators.get(
        statement_type,
        3
    )

    if indicator_count < required:

        return (
            False,
            f"insufficient_statement_indicators_{indicator_count}"
        )

    rows = extract_statement_rows(page_text)

    if len(rows) < 3:
        return False, "insufficient_numerical_rows"

    return True, "heading_and_statement_indicators_verified"


def identify_statement_type(page_text):

    if not page_text or is_rejected_page(page_text):
        return []

    matches = []

    for statement_type in STATEMENT_HEADINGS:

        valid, reason = validate_statement_page(
            page_text,
            statement_type
        )

        if valid:
            matches.append(statement_type)

    return matches


# ==================================================
# NUMERIC EXTRACTION
# ==================================================

NUMBER_PATTERN = re.compile(
    r"""
    (?<![A-Za-z])
    \(?\s*
    -?
    \d[\d,]*(?:\.\d+)?
    \s*\)?
    |
    (?<![A-Za-z])[-–—](?![A-Za-z])
    """,
    re.VERBOSE
)


def extract_numeric_values(text):

    results = []

    for match in NUMBER_PATTERN.finditer(text):

        token = match.group(0).strip()

        if token in ("-", "–", "—"):

            results.append(None)
            continue

        negative = (
            token.startswith("(")
            and token.endswith(")")
        )

        token = (
            token
            .replace(",", "")
            .replace("(", "")
            .replace(")", "")
            .replace(" ", "")
        )

        try:

            number = float(token)

            if number.is_integer():
                number = int(number)

            if negative:
                number = -number

            results.append(number)

        except ValueError:
            continue

    return results


def extract_statement_rows(page_text):

    rows = []

    for line in page_text.splitlines():

        line = line.strip()

        if not line:
            continue

        if re.fullmatch(r"\d{1,4}", line):
            continue

        values = extract_numeric_values(line)

        if not values:
            continue

        if not re.search(r"[A-Za-z]", line):
            continue

        rows.append({
            "label": re.sub(r"\s+", " ", line).strip(),
            "values": values,
            "raw": line
        })

    return rows


# ==================================================
# REPORTING PERIOD
# ==================================================

DATE_PATTERNS = [

    r"(?:as at|as of)\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",

    r"(?:for the year ended|year ended)\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",

    r"(?:six months ended|half year ended)\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",

    r"(?:three months ended|quarter ended)\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",

    r"(?:period ended)\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})"

]


def detect_reporting_period(page_text, document_date):

    normalised = re.sub(
        r"\s+",
        " ",
        page_text or ""
    )

    for pattern in DATE_PATTERNS:

        match = re.search(
            pattern,
            normalised,
            re.IGNORECASE
        )

        if not match:
            continue

        date_text = match.group(1)

        for date_format in (
            "%d %B %Y",
            "%d %b %Y"
        ):

            try:

                parsed = datetime.strptime(
                    date_text,
                    date_format
                )

                return parsed.strftime("%Y-%m-%d")

            except ValueError:
                pass

    return document_date


def get_document_date(item):

    timestamp = item.get("documentDate")

    if timestamp:

        try:

            return datetime.fromtimestamp(
                timestamp / 1000,
                tz=timezone.utc
            ).strftime("%Y-%m-%d")

        except (ValueError, OSError, TypeError):
            pass

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")


def determine_period_type(title):

    title = str(title or "").lower()

    if any(
        word in title
        for word in [
            "half year",
            "half-year",
            "interim",
            "quarterly"
        ]
    ):
        return "interim"

    return "annual"


def determine_fiscal_year(reporting_period):

    try:
        return int(reporting_period[:4])

    except (ValueError, TypeError):
        return None


# ==================================================
# PDF STATEMENT EXTRACTION
# ==================================================

def extract_pdf_statements(
    pdf_path,
    document_date
):

    document = pymupdf.open(str(pdf_path))

    extracted = {}

    try:

        for page_index, page in enumerate(document):

            page_text = page.get_text("text")

            if not page_text.strip():
                continue

            statement_types = identify_statement_type(
                page_text
            )

            if not statement_types:
                continue

            reporting_period = detect_reporting_period(
                page_text,
                document_date
            )

            rows = extract_statement_rows(page_text)

            for statement_type in statement_types:

                valid, validation_reason = validate_statement_page(
                    page_text,
                    statement_type
                )

                if not valid:
                    continue

                key = (
                    statement_type,
                    reporting_period
                )

                if key not in extracted:

                    extracted[key] = {
                        "statement_type": statement_type,
                        "reporting_period": reporting_period,
                        "pages": []
                    }

                extracted[key]["pages"].append({

                    "pdf_page": page_index + 1,

                    "text": page_text,

                    "rows": rows,

                    "page_validation": validation_reason,

                    "indicator_count": count_statement_indicators(
                        page_text,
                        statement_type
                    )

                })

                logger.info(
                    "Accepted %s on PDF page %s for %s.",
                    statement_type,
                    page_index + 1,
                    reporting_period
                )

    finally:

        document.close()

    return list(extracted.values())


# ==================================================
# REPORT PROCESSING
# ==================================================

def process_announcement(
    ticker,
    company_name,
    announcement,
    currency="SGD"
):

    announcement_id = announcement.get("id")
    announcement_url = announcement.get("url")

    if not announcement_id or not announcement_url:
        return 0

    if not is_financial_report(announcement):
        return 0

    document_date = get_document_date(announcement)

    report_folder = (
        REPORT_DIR
        / ticker
        / str(announcement_id)
    )

    pdf_links = get_pdf_links(announcement_url)

    if not pdf_links:

        logger.warning(
            "No PDF found for %s (%s)",
            ticker,
            announcement_id
        )

        return 0

    collected = 0

    for index, pdf_url in enumerate(pdf_links):

        filename = f"report_{index + 1}.pdf"

        pdf_path = report_folder / filename

        try:

            download_pdf(pdf_url, pdf_path)

            statements = extract_pdf_statements(
                pdf_path,
                document_date
            )

            if not statements:

                logger.warning(
                    "No verified statement pages found in %s.",
                    pdf_path.name
                )

                continue

            period_type = determine_period_type(
                announcement.get("title", "")
            )

            for statement in statements:

                reporting_period = statement[
                    "reporting_period"
                ]

                save_financial_statement({

                    "ticker": ticker,

                    "company_name": company_name,

                    "exchange": "SGX",

                    "country": "Singapore",

                    "statement_type": statement[
                        "statement_type"
                    ],

                    "reporting_period": reporting_period,

                    "period_type": period_type,

                    "fiscal_year": determine_fiscal_year(
                        reporting_period
                    ),

                    "currency": currency,

                    "source": SOURCE_NAME,

                    "source_document": pdf_url,

                    "data": {

                        "announcement_id": announcement_id,

                        "announcement_title": announcement.get(
                            "title"
                        ),

                        "announcement_date": document_date,

                        "pdf_path": str(pdf_path),

                        "pdf_url": pdf_url,

                        "unit": "Not yet standardised",

                        "extraction_method": "PDF text extraction",

                        "extraction_status": (
                            "heading_and_statement_indicators_verified"
                        ),

                        "verification_note": (
                            "The page passed statement-heading and "
                            "statement-specific indicator checks. "
                            "Raw numerical values have not yet been "
                            "standardised or independently verified."
                        ),

                        "pages": statement["pages"]

                    }

                })

                collected += 1

        except Exception as error:

            logger.exception(
                "Failed processing PDF for %s: %s",
                ticker,
                error
            )

    return collected


# ==================================================
# COMPANY COLLECTION
# ==================================================

def collect_company_statements(
    ticker,
    max_pages=60
):

    ticker = ticker.upper().strip()

    started_at = datetime.now(
        timezone.utc
    ).isoformat()

    try:

        initialise_statement_database()

        company = get_sgx_company(ticker)

        if not company:

            raise ValueError(
                f"{ticker} was not found in the SGX market universe."
            )

        company_name = company.get("company_name")

        if not company_name:

            raise ValueError(
                f"No company name found for {ticker}."
            )

        currency = company.get("currency") or "SGD"

        logger.info(
            "Starting SGX collection: %s - %s",
            ticker,
            company_name
        )

        announcements = get_sgx_announcements(
            max_pages=max_pages
        )

        company_announcements = match_company_announcements(
            announcements,
            company_name
        )

        reports = [
            item
            for item in company_announcements
            if is_financial_report(item)
        ]

        logger.info(
            "Found %s potential financial reports for %s.",
            len(reports),
            ticker
        )

        total_collected = 0

        for announcement in reports:

            total_collected += process_announcement(
                ticker,
                company_name,
                announcement,
                currency
            )

        status = (
            "success"
            if total_collected > 0
            else "no_statements_found"
        )

        record_collection_history(
            ticker=ticker,
            status=status,
            statements_collected=total_collected,
            started_at=started_at
        )

        return {

            "ticker": ticker,

            "company_name": company_name,

            "status": status,

            "announcements_found": len(
                company_announcements
            ),

            "financial_reports_found": len(reports),

            "statements_collected": total_collected,

            "source": SOURCE_NAME

        }

    except Exception as error:

        logger.exception(
            "SGX collection failed for %s",
            ticker
        )

        record_collection_history(
            ticker=ticker,
            status="failed",
            error_message=str(error),
            started_at=started_at
        )

        return {
            "ticker": ticker,
            "status": "failed",
            "error": str(error)
        }


# ==================================================
# DATABASE SUMMARY
# ==================================================

def get_sgx_collection_summary():

    with get_connection() as conn:

        rows = conn.execute(
            """
            SELECT
                statement_type,
                period_type,
                COUNT(*) AS total
            FROM financial_statements
            WHERE exchange = 'SGX'
            GROUP BY statement_type, period_type
            ORDER BY statement_type
            """
        ).fetchall()

        companies = conn.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM financial_statements
            WHERE exchange = 'SGX'
            """
        ).fetchone()[0]

        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM financial_statements
            WHERE exchange = 'SGX'
            """
        ).fetchone()[0]

    return {

        "total_statements": total,

        "companies_covered": companies,

        "breakdown": [
            dict(row)
            for row in rows
        ]

    }


# ==================================================
# DIRECT EXECUTION
# ==================================================

if __name__ == "__main__":

    result = collect_company_statements("D05")

    print(
        json.dumps(
            result,
            indent=4
        )
    )
