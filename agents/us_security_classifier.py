"""Build non-destructive, evidence-backed US security classification candidates."""

import argparse
import csv
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse


BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = BASE_DIR / "database" / "assistant.db"
US_DATA_DIR = BASE_DIR / "data" / "market_data" / "us"
HKEX_DATA_DIR = BASE_DIR / "data" / "market_data" / "hkex"
DEFAULT_EVIDENCE_PATH = US_DATA_DIR / "security_classification_evidence.csv"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us_security_classification"
PRIOR_PHASE2_OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us_security_classification_v2"
PHASE2_OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us_security_classification_v3"
PILOT_OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us_pilot_20"

ISSUER_STRUCTURES = {
    "operating_company",
    "reit",
    "etf",
    "open_end_fund",
    "closed_end_fund",
    "business_development_company",
    "unit_investment_trust",
    "partnership",
    "other",
    "unknown",
}

SECURITY_FORMS = {
    "common_share",
    "adr_ads",
    "preferred_share",
    "fund_share",
    "exchange_traded_product",
    "warrant",
    "unit",
    "rights",
    "debt_note",
    "exchange_traded_note",
    "partnership_unit",
    "other",
    "unknown",
}

EVIDENCE_FIELDS = [
    "ticker",
    "exchange",
    "sec_cik",
    "issuer_structure",
    "security_form",
    "share_class",
    "source_authority",
    "evidence_source",
    "evidence_reference",
    "evidence_date",
    "rationale",
]

CANDIDATE_FIELDS = [
    "listing_id",
    "ticker",
    "exchange",
    "company_name",
    "country",
    "currency",
    "sec_cik",
    "is_active",
    "existing_security_type",
    "identity_evidence_source",
    "issuer_structure",
    "security_form",
    "share_class",
    "classification_status",
    "classification_confidence",
    "evidence_source",
    "evidence_reference",
    "classification_rationale",
    "classification_date",
    "manual_review_status",
    "manual_review_reason",
    "candidate_hints",
    "secondary_suggestions",
]


def normalize_cik(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.isdigit() or len(raw) > 10:
        raise ValueError(f"Invalid CIK in security-classification evidence: {value!r}")
    return raw.zfill(10)


def listing_key(record):
    """Match security-level evidence without collapsing case-distinct symbols."""

    return (
        str(record.get("ticker") or "").strip(),
        str(record.get("exchange") or "").strip().upper(),
    )


def load_us_listings(database_path=None):
    """Read each US listing independently using SQLite URI read-only mode."""

    path = Path(database_path) if database_path else DATABASE_PATH
    if not path.exists():
        raise FileNotFoundError(f"Market-universe database not found: {path}")
    uri = "file:" + quote(str(path.resolve()), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, ticker, company_name, exchange, country, currency,
                   security_type, is_active, data_source, sec_cik
            FROM market_universe
            WHERE country = 'USA'
            ORDER BY exchange, ticker, id
            """
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def load_evidence_csv(path):
    """Load pre-collected evidence; this function performs no network access."""

    evidence_path = Path(path)
    if not evidence_path.exists():
        return []
    with evidence_path.open("r", encoding="utf-8-sig", newline="") as evidence_file:
        reader = csv.DictReader(evidence_file)
        missing = set(EVIDENCE_FIELDS).difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Security evidence CSV is missing columns: "
                + ", ".join(sorted(missing))
            )
        return [normalize_evidence(row, index) for index, row in enumerate(reader, 2)]


def normalize_evidence(row, row_number=None):
    prefix = f"Evidence row {row_number}: " if row_number else "Evidence row: "
    ticker = str(row.get("ticker") or "").strip()
    exchange = str(row.get("exchange") or "").strip().upper()
    cik = normalize_cik(row.get("sec_cik"))
    if not ticker or not exchange:
        raise ValueError(prefix + "ticker and exchange are required for exact listing matching.")

    authority = str(row.get("source_authority") or "").strip().upper()
    if authority not in {"SEC", "EXCHANGE", "ISSUER_IR", "SECONDARY"}:
        raise ValueError(prefix + "source_authority must be SEC, EXCHANGE, ISSUER_IR, or SECONDARY.")

    structure = str(row.get("issuer_structure") or "unknown").strip().lower()
    form = str(row.get("security_form") or "unknown").strip().lower()
    if structure not in ISSUER_STRUCTURES:
        raise ValueError(prefix + f"unsupported issuer_structure: {structure}")
    if form not in SECURITY_FORMS:
        raise ValueError(prefix + f"unsupported security_form: {form}")

    normalized = {
        "ticker": ticker,
        "exchange": exchange,
        "sec_cik": cik,
        "issuer_structure": structure,
        "security_form": form,
        "share_class": str(row.get("share_class") or "").strip(),
        "source_authority": authority,
        "evidence_source": str(row.get("evidence_source") or "").strip(),
        "evidence_reference": str(row.get("evidence_reference") or "").strip(),
        "evidence_date": str(row.get("evidence_date") or "").strip(),
        "rationale": str(row.get("rationale") or "").strip(),
    }
    if not normalized["evidence_source"] or not normalized["evidence_reference"]:
        raise ValueError(prefix + "evidence_source and evidence_reference are required.")
    if authority in {"SEC", "EXCHANGE"} and (
        not normalized["evidence_date"] or not normalized["rationale"]
    ):
        raise ValueError(prefix + "primary SEC/exchange evidence requires evidence_date and rationale.")
    if authority in {"SEC", "EXCHANGE"}:
        parsed = urlparse(normalized["evidence_reference"])
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            raise ValueError(prefix + "primary evidence_reference must be an official HTTPS URL.")
        if authority == "SEC" and not (host == "sec.gov" or host.endswith(".sec.gov")):
            raise ValueError(prefix + "SEC evidence must link to an sec.gov source.")
        if authority == "EXCHANGE" and not any(
            host == domain or host.endswith("." + domain)
            for domain in ("nasdaq.com", "nasdaqtrader.com", "nyse.com", "nysegroup.com")
        ):
            raise ValueError(prefix + "Exchange evidence must link to an official Nasdaq or NYSE source.")
    return normalized


def candidate_hints(listing):
    """Return low-confidence review hints; hints never establish a class."""

    name = str(listing.get("company_name") or "")
    ticker = str(listing.get("ticker") or "").upper()
    combined = name.upper()
    hints = []
    rules = (
        (r"\b(ETF|EXCHANGE[- ]TRADED FUND)\b", "possible_etf"),
        (r"\b(ADR|ADS|AMERICAN DEPOSITARY|DEPOSITORY RECEIPT)\b", "possible_depositary_share"),
        (r"\b(PREFERRED|PREFERENCE)\b", "possible_preferred_share"),
        (r"\bWARRANT\b", "possible_warrant"),
        (r"\bRIGHTS?\b", "possible_rights"),
        (r"\bUNITS?\b", "possible_unit"),
        (r"\b(REIT|REAL ESTATE INVESTMENT TRUST)\b", "possible_reit_issuer"),
        (r"\b(CLOSED[- ]END|MUTUAL|INVESTMENT) FUND\b", "possible_fund_issuer"),
        (r"\bBUSINESS DEVELOPMENT COMPANY\b|\bBDC\b", "possible_bdc_issuer"),
    )
    for pattern, hint in rules:
        if re.search(pattern, combined):
            hints.append(hint)
    if re.search(r"(?:[-.]WT|[-.]WS|[-.]W)$", ticker):
        hints.append("possible_warrant_ticker_suffix")
    if re.search(r"(?:[-.]UN|[-.]U)$", ticker):
        hints.append("possible_unit_ticker_suffix")
    return sorted(set(hints))


def _consensus(evidence, field):
    values = sorted({row[field] for row in evidence if row[field] != "unknown"})
    if len(values) > 1:
        return "unknown", True
    return (values[0] if values else "unknown"), False


def classify_listing(listing, evidence_rows=None, classification_date=None):
    """Classify one listing; issuer CIK is a link, not the grouping key."""

    evidence_rows = evidence_rows or []
    key = listing_key(listing)
    matching = [row for row in evidence_rows if listing_key(row) == key]
    primary_candidates = [row for row in matching if row["source_authority"] in {"SEC", "EXCHANGE"}]
    cik = normalize_cik(listing.get("sec_cik"))
    identity_conflict = any(
        row["sec_cik"] and cik and row["sec_cik"] != cik
        for row in primary_candidates
    )
    primary = [
        row for row in primary_candidates
        if not row["sec_cik"] or not cik or row["sec_cik"] == cik
    ]
    supporting = [row for row in matching if row["source_authority"] in {"ISSUER_IR", "SECONDARY"}]

    structure, structure_conflict = _consensus(primary, "issuer_structure")
    form, form_conflict = _consensus(primary, "security_form")
    share_classes = sorted({row["share_class"] for row in primary if row["share_class"]})
    class_conflict = len(share_classes) > 1
    share_class = share_classes[0] if len(share_classes) == 1 else ""

    hints = candidate_hints(listing)
    supporting_suggestions = sorted({
        value
        for row in supporting
        for value in (row["issuer_structure"], row["security_form"])
        if value != "unknown"
    })
    reasons = []
    if not primary:
        reasons.append("No exact-listing SEC or exchange evidence was supplied.")
    if structure_conflict:
        reasons.append("Conflicting primary issuer-structure evidence.")
    elif structure == "unknown":
        reasons.append("Issuer structure is unconfirmed.")
    if form_conflict:
        reasons.append("Conflicting primary security-form evidence.")
    elif form == "unknown":
        reasons.append("Security form is unconfirmed.")
    if class_conflict:
        reasons.append("Conflicting primary share-class evidence.")
    if identity_conflict:
        reasons.append("Primary evidence CIK conflicts with the listing issuer link.")
    if supporting and not primary:
        reasons.append("Supporting/secondary evidence cannot establish classification by itself.")
    if hints and not primary:
        reasons.append("Name or ticker hints are leads only and require primary-source review.")

    complete = (
        bool(primary)
        and structure != "unknown"
        and form != "unknown"
        and not structure_conflict
        and not form_conflict
        and not class_conflict
        and not identity_conflict
    )
    if structure_conflict or form_conflict or class_conflict or identity_conflict:
        confidence = "conflicting"
        status = "conflicting"
        review_status = "required"
    elif complete:
        confidence = "high"
        status = "confirmed"
        review_status = "not_required"
    elif primary and (structure != "unknown" or form != "unknown"):
        confidence = "medium"
        status = "provisional"
        review_status = "required"
    else:
        confidence = "low"
        status = "unknown"
        review_status = "required"

    sources = sorted({row["evidence_source"] for row in matching if row["evidence_source"]})
    references = sorted({row["evidence_reference"] for row in matching if row["evidence_reference"]})
    rationales = sorted({row["rationale"] for row in primary if row["rationale"]})
    if not rationales:
        rationales = reasons or ["Primary source confirms this listing classification."]

    return {
        "listing_id": listing.get("id", ""),
        "ticker": listing.get("ticker", ""),
        "exchange": listing.get("exchange", ""),
        "company_name": listing.get("company_name", ""),
        "country": listing.get("country", "USA"),
        "currency": listing.get("currency", ""),
        "sec_cik": cik,
        "is_active": listing.get("is_active", ""),
        "existing_security_type": listing.get("security_type", ""),
        "identity_evidence_source": listing.get("data_source", ""),
        "issuer_structure": structure,
        "security_form": form,
        "share_class": share_class,
        "classification_status": status,
        "classification_confidence": confidence,
        "evidence_source": ";".join(sources),
        "evidence_reference": ";".join(references),
        "classification_rationale": " ".join(rationales),
        "classification_date": classification_date or datetime.now(timezone.utc).date().isoformat(),
        "manual_review_status": review_status,
        "manual_review_reason": " ".join(reasons),
        "candidate_hints": ";".join(hints),
        "secondary_suggestions": ";".join(supporting_suggestions),
    }


def classify_listings(listings, evidence_rows=None, classification_date=None):
    evidence_rows = evidence_rows or []
    return [
        classify_listing(listing, evidence_rows, classification_date)
        for listing in listings
    ]


def build_report(listings, evidence_rows, candidates, evidence_path, generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    counts = Counter(row["classification_status"] for row in candidates)
    structures = Counter(row["issuer_structure"] for row in candidates)
    forms = Counter(row["security_form"] for row in candidates)
    hints = Counter(
        hint
        for row in candidates
        for hint in row["candidate_hints"].split(";")
        if hint
    )
    exact_keys = {listing_key(row) for row in listings}
    primary_keys = {
        listing_key(row)
        for row in evidence_rows
        if row["source_authority"] in {"SEC", "EXCHANGE"}
    }
    lines = [
        "US SECURITY CLASSIFICATION CANDIDATE REPORT",
        "=" * 48,
        f"Generated at UTC: {generated_at}",
        f"US listing rows evaluated: {len(listings)}",
        f"Distinct ticker/exchange listing keys: {len(exact_keys)}",
        f"Primary SEC/exchange evidence rows loaded: {sum(row['source_authority'] in {'SEC', 'EXCHANGE'} for row in evidence_rows)}",
        f"Listings with exact-key primary evidence: {len(exact_keys.intersection(primary_keys))}",
        f"Evidence file: {evidence_path}",
        "Database access: read-only; no market-universe records imported or changed.",
        "Special instruments are retained as candidates; candidate hints are not classifications.",
        "",
        "CLASSIFICATION STATUS",
        "-" * 48,
    ]
    for label in ("confirmed", "provisional", "conflicting", "unknown"):
        lines.append(f"{label}: {counts.get(label, 0)}")
    lines.extend(["", "ISSUER STRUCTURE", "-" * 48])
    lines.extend(f"{name}: {count}" for name, count in sorted(structures.items()))
    lines.extend(["", "SECURITY FORM", "-" * 48])
    lines.extend(f"{name}: {count}" for name, count in sorted(forms.items()))
    lines.extend(["", "CANDIDATE HINTS (NOT CONFIRMATION)", "-" * 48])
    lines.extend(f"{name}: {count}" for name, count in sorted(hints.items()))
    if not hints:
        lines.append("None")
    return "\n".join(lines) + "\n"


def validate_output_dir(output_dir, protected_paths=None):
    """Reject output destinations that could touch protected production data."""

    target = Path(output_dir).resolve()
    protected = protected_paths or (
        US_DATA_DIR,
        HKEX_DATA_DIR,
        DEFAULT_OUTPUT_DIR,
        PRIOR_PHASE2_OUTPUT_DIR,
        PILOT_OUTPUT_DIR,
        DATABASE_PATH,
    )
    for protected_path in protected:
        protected = Path(protected_path).resolve()
        try:
            common = Path(os.path.commonpath((str(target), str(protected))))
        except ValueError:  # Different Windows drives cannot overlap.
            continue
        if os.path.normcase(str(common)) in {
            os.path.normcase(str(target)), os.path.normcase(str(protected))
        }:
            raise ValueError(
                f"Classification output directory overlaps protected path: {protected}"
            )
    return target


def _write_csv_new(path, rows, fields):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing classification output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"Refusing to overwrite temporary output: {temporary}")
    with temporary.open("x", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    if path.exists():
        temporary.unlink()
        raise FileExistsError(f"Refusing to overwrite existing classification output: {path}")
    os.replace(temporary, path)


def _write_text_new(path, content):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing classification output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def run_pipeline(database_path=None, evidence_path=None, output_dir=None, classification_date=None):
    listings = load_us_listings(database_path)
    evidence_path = Path(evidence_path) if evidence_path else DEFAULT_EVIDENCE_PATH
    evidence_rows = load_evidence_csv(evidence_path)
    candidates = classify_listings(listings, evidence_rows, classification_date)
    output_dir = validate_output_dir(output_dir or DEFAULT_OUTPUT_DIR)
    all_path = output_dir / "us_security_classification_candidates.csv"
    manual_path = output_dir / "us_security_classification_manual_review.csv"
    report_path = output_dir / "us_security_classification_report.txt"
    manual = [row for row in candidates if row["manual_review_status"] == "required"]
    report = build_report(listings, evidence_rows, candidates, evidence_path)
    # Check every destination first so an existing file stops the run before
    # any part of the candidate set is written.
    for path in (all_path, manual_path, report_path):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing classification output: {path}")
    _write_csv_new(all_path, candidates, CANDIDATE_FIELDS)
    _write_csv_new(manual_path, manual, CANDIDATE_FIELDS)
    _write_text_new(report_path, report)
    return {
        "candidate_count": len(candidates),
        "manual_review_count": len(manual),
        "confirmed_count": sum(row["classification_status"] == "confirmed" for row in candidates),
        "candidate_csv": all_path,
        "manual_review_csv": manual_path,
        "report": report_path,
        "report_text": report,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build US security-classification candidates without database writes.")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--evidence-csv", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    result = run_pipeline(args.database, args.evidence_csv, args.output_dir)
    print("US security-classification candidates generated")
    print(f"Listings evaluated: {result['candidate_count']}")
    print(f"Manual review required: {result['manual_review_count']}")
    print(f"Confirmed: {result['confirmed_count']}")
    print(f"Candidate CSV: {result['candidate_csv']}")
    print(f"Manual-review CSV: {result['manual_review_csv']}")
    print(f"Report: {result['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
