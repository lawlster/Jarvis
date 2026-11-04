
import csv
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data" / "market_data" / "hkex"

INPUT_FILE = DATA_DIR / "classified_securities.csv"

OUTPUT_CANDIDATES = DATA_DIR / "hkex_normalized_candidates.csv"
OUTPUT_SECONDARY = DATA_DIR / "hkex_secondary_counters.csv"
OUTPUT_MANUAL = DATA_DIR / "hkex_manual_review.csv"
OUTPUT_REPORT = DATA_DIR / "hkex_normalization_report.txt"

PRIMARY_CLASSIFICATIONS = {
    "include_candidate",
    "include",
}

MANUAL_CLASSIFICATION = "manual_review"


# ============================================================
# HELPERS
# ============================================================

def normalize_stock_code(value):
    value = str(value or "").strip()

    if not value:
        return ""

    if value.isdigit():
        return value.zfill(5)

    return value


def clean(value):
    return str(value or "").strip()


def is_wvr_security(name):
    """
    HKEX names ending in -W indicate weighted voting rights.
    This is retained as a governance flag, not an exclusion.
    """
    return bool(re.search(r"-W$", clean(name), re.IGNORECASE))


def write_csv(path, rows, original_fields, extra_fields):
    fieldnames = original_fields + extra_fields

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)


# ============================================================
# MAIN NORMALIZATION
# ============================================================

def main():

    print("=" * 55)
    print("AiAssistant - HKEX Universe Normalizer")
    print("=" * 55)

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE}"
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with INPUT_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)
        original_fields = reader.fieldnames or []
        records = list(reader)

    required_fields = {
        "stock_code",
        "name",
        "category",
        "subcategory",
        "isin",
        "trading_currency",
        "rmb_counter",
        "classification",
    }

    missing_fields = required_fields - set(original_fields)

    if missing_fields:
        raise ValueError(
            f"Missing required CSV columns: {sorted(missing_fields)}"
        )

    print(f"\nInput records: {len(records):,}")

    # --------------------------------------------------------
    # Build stock-code lookup
    # --------------------------------------------------------

    records_by_code = {}

    for row in records:
        code = normalize_stock_code(row["stock_code"])

        if code:
            records_by_code[code] = row

    # --------------------------------------------------------
    # Identify explicitly linked RMB counters
    # --------------------------------------------------------

    primary_to_secondary = {}
    secondary_to_primary = {}
    link_warnings = []

    for row in records:

        primary_code = normalize_stock_code(row["stock_code"])
        secondary_code = normalize_stock_code(row["rmb_counter"])

        if not primary_code or not secondary_code:
            continue

        if clean(row["trading_currency"]).upper() != "HKD":
            continue

        secondary = records_by_code.get(secondary_code)

        if secondary is None:
            link_warnings.append(
                f"{primary_code}: RMB counter {secondary_code} "
                "was not found in the source CSV"
            )
            continue

        if clean(secondary["trading_currency"]).upper() != "RMB":
            link_warnings.append(
                f"{primary_code}: linked counter {secondary_code} "
                "does not have RMB trading currency"
            )
            continue

        primary_isin = clean(row["isin"]).upper()
        secondary_isin = clean(secondary["isin"]).upper()

        if (
            primary_isin
            and secondary_isin
            and primary_isin != secondary_isin
        ):
            link_warnings.append(
                f"{primary_code}/{secondary_code}: ISIN mismatch"
            )
            continue

        primary_to_secondary[primary_code] = secondary_code
        secondary_to_primary[secondary_code] = primary_code

    # --------------------------------------------------------
    # Separate classifications
    # --------------------------------------------------------

    candidates = []
    secondary_counters = []
    manual_records = []

    excluded_count = 0
    unknown_classifications = []

    for row in records:

        classification = clean(row["classification"])
        stock_code = normalize_stock_code(row["stock_code"])

        if classification == MANUAL_CLASSIFICATION:
            manual_records.append(dict(row))
            continue

        if classification not in PRIMARY_CLASSIFICATIONS:
            if classification == "exclude":
                excluded_count += 1
            else:
                unknown_classifications.append(
                    f"{stock_code}: {classification}"
                )
            continue

        # An RMB counter is secondary only when an explicit,
        # validated HKD-to-RMB relationship exists.

        if stock_code in secondary_to_primary:

            primary_code = secondary_to_primary[stock_code]

            secondary_row = dict(row)

            secondary_row.update({
                "primary_stock_code": primary_code,
                "counter_relationship": "secondary_rmb_counter",
            })

            secondary_counters.append(secondary_row)
            continue

        # All remaining eligible records are retained as
        # independent company/security candidates.

        normalized = dict(row)

        linked_counter = primary_to_secondary.get(stock_code, "")

        isin = clean(row["isin"]).upper()

        if isin:
            security_identity = isin
        else:
            security_identity = f"HKEX:{stock_code}"

        review_flags = []

        if not isin:
            review_flags.append("missing_isin")

        if not linked_counter and clean(row["rmb_counter"]):
            review_flags.append("unresolved_rmb_counter")

        if classification == "include":
            if clean(row["category"]) == "Real Estate Investment Trusts":
                review_flags.append("reit_valuation_required")

        normalized.update({
            "security_identity": security_identity,
            "primary_stock_code": stock_code,
            "linked_rmb_counter": linked_counter,
            "is_primary_counter": "Y",
            "is_wvr_security": (
                "Y" if is_wvr_security(row["name"]) else "N"
            ),
            "review_required": "Y" if review_flags else "N",
            "review_flags": ";".join(review_flags),
        })

        candidates.append(normalized)

    # --------------------------------------------------------
    # Duplicate identity analysis
    # --------------------------------------------------------

    identity_groups = defaultdict(list)

    for row in candidates:
        identity = clean(row["security_identity"])

        if identity:
            identity_groups[identity].append(row)

    duplicate_identity_groups = {
        identity: group
        for identity, group in identity_groups.items()
        if len(group) > 1
    }

    # --------------------------------------------------------
    # Output files
    # --------------------------------------------------------

    candidate_extra_fields = [
        "security_identity",
        "primary_stock_code",
        "linked_rmb_counter",
        "is_primary_counter",
        "is_wvr_security",
        "review_required",
        "review_flags",
    ]

    secondary_extra_fields = [
        "primary_stock_code",
        "counter_relationship",
    ]

    write_csv(
        OUTPUT_CANDIDATES,
        candidates,
        original_fields,
        candidate_extra_fields,
    )

    write_csv(
        OUTPUT_SECONDARY,
        secondary_counters,
        original_fields,
        secondary_extra_fields,
    )

    write_csv(
        OUTPUT_MANUAL,
        manual_records,
        original_fields,
        [],
    )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    classification_counts = Counter(
        clean(row["classification"])
        for row in records
    )

    category_counts = Counter(
        clean(row["category"])
        for row in candidates
    )

    missing_isin_candidates = [
        row for row in candidates
        if not clean(row["isin"])
    ]

    wvr_count = sum(
        row["is_wvr_security"] == "Y"
        for row in candidates
    )

    reit_count = sum(
        clean(row["category"]) == "Real Estate Investment Trusts"
        for row in candidates
    )

    report_lines = [
        "HKEX UNIVERSE NORMALIZATION REPORT",
        "=" * 55,
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "SOURCE",
        f"Input file: {INPUT_FILE}",
        f"Total source records: {len(records)}",
        "",
        "SOURCE CLASSIFICATIONS",
    ]

    for classification, count in sorted(classification_counts.items()):
        report_lines.append(f"{classification}: {count}")

    report_lines.extend([
        "",
        "NORMALIZATION RESULTS",
        f"Primary candidates: {len(candidates)}",
        f"Secondary RMB counters: {len(secondary_counters)}",
        f"Manual review records: {len(manual_records)}",
        f"Excluded records: {excluded_count}",
        f"Unknown classifications: {len(unknown_classifications)}",
        "",
        "CANDIDATE CATEGORY TOTALS",
    ])

    for category, count in sorted(category_counts.items()):
        report_lines.append(f"{category}: {count}")

    report_lines.extend([
        "",
        "DATA QUALITY",
        f"Candidates missing ISIN: {len(missing_isin_candidates)}",
        f"WVR securities: {wvr_count}",
        f"REIT candidates: {reit_count}",
        f"Duplicate candidate identity groups: {len(duplicate_identity_groups)}",
        f"RMB counter link warnings: {len(link_warnings)}",
        "",
        "MISSING ISIN CANDIDATES",
    ])

    for row in missing_isin_candidates:
        report_lines.append(
            f"{row['stock_code']} | {row['name']} | "
            f"{row['category']} | {row['subcategory']}"
        )

    report_lines.extend([
        "",
        "DUPLICATE CANDIDATE IDENTITIES",
    ])

    for identity, group in duplicate_identity_groups.items():
        report_lines.append(f"\nIdentity: {identity}")

        for row in group:
            report_lines.append(
                f"  {row['stock_code']} | {row['name']}"
            )

    report_lines.extend([
        "",
        "RMB COUNTER LINK WARNINGS",
    ])

    if link_warnings:
        report_lines.extend(link_warnings)
    else:
        report_lines.append("None")

    report_lines.extend([
        "",
        "UNKNOWN CLASSIFICATIONS",
    ])

    if unknown_classifications:
        report_lines.extend(unknown_classifications)
    else:
        report_lines.append("None")

    report_lines.extend([
        "",
        "OUTPUT FILES",
        str(OUTPUT_CANDIDATES),
        str(OUTPUT_SECONDARY),
        str(OUTPUT_MANUAL),
        str(OUTPUT_REPORT),
        "",
        "Database modified: NO",
        "Original classified CSV modified: NO",
        "",
        "NORMALIZATION COMPLETED",
    ])

    report_text = "\n".join(report_lines)

    OUTPUT_REPORT.write_text(
        report_text,
        encoding="utf-8",
    )

    print("\n" + report_text)
    print("\n" + "=" * 55)
    print("NORMALIZATION COMPLETED")
    print("=" * 55)


if __name__ == "__main__":
    main()
