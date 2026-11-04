
import csv
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data" / "market_data" / "hkex"

INPUT_PATH = DATA_DIR / "classified_securities.csv"

FLAGS_PATH = DATA_DIR / "hkex_validation_flags.csv"

REPORT_PATH = DATA_DIR / "hkex_validation_report.txt"

EXPECTED_MINIMUM_RECORDS = 1000


# ==================================================
# LOAD CLASSIFIED SECURITIES
# ==================================================

def load_securities():

    if not INPUT_PATH.exists():
        raise FileNotFoundError(
            f"Classification file not found: {INPUT_PATH}"
        )

    with open(
        INPUT_PATH,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        reader = csv.DictReader(file)

        required_columns = {
            "stock_code",
            "name",
            "category",
            "subcategory",
            "isin",
            "trading_currency",
            "classification",
        }

        missing = required_columns - set(reader.fieldnames or [])

        if missing:
            raise ValueError(
                f"Missing required CSV columns: {sorted(missing)}"
            )

        return list(reader)


# ==================================================
# INITIAL DATA CHECKS
# ==================================================

def validate_securities(securities):

    stock_codes = defaultdict(list)
    isins = defaultdict(list)

    category_counts = Counter()
    classification_counts = Counter()
    currency_counts = Counter()

    for index, security in enumerate(securities):

        stock_code = security["stock_code"].strip()
        isin = security["isin"].strip().upper()

        if stock_code:
            stock_codes[stock_code].append(index)

        if isin:
            isins[isin].append(index)

        category_counts[security["category"].strip()] += 1
        classification_counts[
            security["classification"].strip()
        ] += 1

        currency = security["trading_currency"].strip()

        currency_counts[currency or "MISSING"] += 1

    duplicate_stock_codes = {
        code for code, indexes in stock_codes.items()
        if len(indexes) > 1
    }

    duplicate_isins = {
        isin for isin, indexes in isins.items()
        if len(indexes) > 1
    }

    return (
        duplicate_stock_codes,
        duplicate_isins,
        category_counts,
        classification_counts,
        currency_counts,
    )


# ==================================================
# VALIDATE INDIVIDUAL RECORDS
# ==================================================

def validate_record(security, duplicate_stock_codes, duplicate_isins):

    flags = []
    errors = []

    stock_code = security["stock_code"].strip()
    name = security["name"].strip()
    isin = security["isin"].strip().upper()
    currency = security["trading_currency"].strip()
    category = security["category"].strip()
    subcategory = security["subcategory"].strip()
    classification = security["classification"].strip()

    # Stock code checks

    if not stock_code:
        errors.append("missing_stock_code")

    elif not stock_code.isdigit() or len(stock_code) != 5:
        errors.append("invalid_stock_code")

    if stock_code in duplicate_stock_codes:
        errors.append("duplicate_stock_code")

    # Company name checks

    if not name:
        errors.append("missing_name")

    # ISIN checks

    if not isin:
        errors.append("missing_isin")

    elif len(isin) != 12 or not isin.isalnum():
        errors.append("invalid_isin_format")

    if isin in duplicate_isins:
        flags.append("duplicate_isin_review")

    # Currency checks

    if not currency:
        flags.append("missing_currency")

    # Classification checks

    if classification == "include_candidate":

        if category != "Equity":
            errors.append("equity_category_mismatch")

        if subcategory not in {
            "Equity Securities (Main Board)",
            "Equity Securities (GEM)",
        }:
            errors.append("unexpected_equity_subcategory")

    elif classification == "include":

        if category != "Real Estate Investment Trusts":
            errors.append("reit_category_mismatch")

    elif classification == "exclude":

        if category not in {
            "Derivative Warrants",
            "Callable Bull/Bear Contracts",
            "Debt Securities",
            "Exchange Traded Products",
            "Equity Warrants (Main Board)",
            "Equity Warrants (GEM)",
        }:
            flags.append("exclusion_rule_review")

    elif classification == "manual_review":

        flags.append("manual_classification_review")

    else:

        errors.append("unknown_classification")

    # Additional review flags

    if category == "Equity" and subcategory == "":

        flags.append("missing_equity_subcategory")

    if category == "Real Estate Investment Trusts":
        flags.append("reit_valuation_method_required")

    if security.get("expiry_date", "").strip():
        flags.append("expiry_date_present_review")

    if errors:
        status = "error"

    elif flags:
        status = "warning"

    else:
        status = "pass"

    return status, errors, flags


# ==================================================
# GENERATE VALIDATION RESULTS
# ==================================================

def generate_results(securities):

    duplicate_stock_codes, duplicate_isins, _, _, _ = (
        validate_securities(securities)
    )

    results = []

    for security in securities:

        status, errors, flags = validate_record(
            security,
            duplicate_stock_codes,
            duplicate_isins
        )

        result = dict(security)

        result["validation_status"] = status
        result["validation_errors"] = ";".join(errors)
        result["validation_flags"] = ";".join(flags)

        results.append(result)

    return results


# ==================================================
# EXPORT FLAGGED RECORDS
# ==================================================

def export_flags(results):

    flagged = [
        item for item in results
        if item["validation_status"] != "pass"
    ]

    if not results:
        raise RuntimeError("No validation results to export.")

    fieldnames = list(results[0].keys())

    with open(
        FLAGS_PATH,
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(flagged)

    return flagged


# ==================================================
# GENERATE REPORT
# ==================================================

def generate_report(securities, results):

    (
        duplicate_stock_codes,
        duplicate_isins,
        category_counts,
        classification_counts,
        currency_counts,
    ) = validate_securities(securities)

    status_counts = Counter(
        item["validation_status"] for item in results
    )

    error_counts = Counter()
    flag_counts = Counter()

    for item in results:

        for error in item["validation_errors"].split(";"):
            if error:
                error_counts[error] += 1

        for flag in item["validation_flags"].split(";"):
            if flag:
                flag_counts[flag] += 1

    lines = []

    lines.append("HKEX UNIVERSE VALIDATION REPORT")
    lines.append("=" * 45)

    lines.append(
        f"Generated: {datetime.now().isoformat(timespec='seconds')}"
    )

    lines.append(f"Total records: {len(securities)}")

    lines.append("\nVALIDATION STATUS")

    for status, count in status_counts.most_common():
        lines.append(f"{status}: {count}")

    lines.append("\nCLASSIFICATION TOTALS")

    for classification, count in classification_counts.most_common():
        lines.append(f"{classification}: {count}")

    lines.append("\nCATEGORY TOTALS")

    for category, count in category_counts.most_common():
        lines.append(f"{category or 'MISSING'}: {count}")

    lines.append("\nCURRENCY TOTALS")

    for currency, count in currency_counts.most_common():
        lines.append(f"{currency}: {count}")

    lines.append("\nDUPLICATE CHECKS")

    lines.append(
        f"Duplicate stock codes: {len(duplicate_stock_codes)}"
    )

    lines.append(
        f"Duplicate ISINs: {len(duplicate_isins)}"
    )

    if duplicate_stock_codes:
        lines.append(
            "Stock codes: " +
            ", ".join(sorted(duplicate_stock_codes))
        )

    if duplicate_isins:
        lines.append(
            "ISINs: " +
            ", ".join(sorted(duplicate_isins))
        )

    lines.append("\nVALIDATION ERRORS")

    if error_counts:

        for error, count in error_counts.most_common():
            lines.append(f"{error}: {count}")

    else:

        lines.append("None")

    lines.append("\nREVIEW FLAGS")

    if flag_counts:

        for flag, count in flag_counts.most_common():
            lines.append(f"{flag}: {count}")

    else:

        lines.append("None")

    lines.append("\nSAMPLE FLAGGED RECORDS")

    shown = 0

    for item in results:

        if item["validation_status"] == "pass":
            continue

        lines.append(
            f'{item["stock_code"]} | '
            f'{item["name"]} | '
            f'{item["validation_status"]} | '
            f'{item["validation_errors"]} | '
            f'{item["validation_flags"]}'
        )

        shown += 1

        if shown >= 20:
            break

    lines.append("\nOUTPUT FILES")
    lines.append(str(FLAGS_PATH))
    lines.append(str(REPORT_PATH))

    REPORT_PATH.write_text(
        "\n".join(lines),
        encoding="utf-8"
    )

    print("\n".join(lines))


# ==================================================
# MAIN
# ==================================================

def main():

    print("======================================")
    print("AiAssistant - HKEX Universe Validator")
    print("======================================")

    try:

        securities = load_securities()

        if len(securities) < EXPECTED_MINIMUM_RECORDS:
            raise RuntimeError(
                f"Only {len(securities)} records loaded. "
                "Validation stopped."
            )

        results = generate_results(securities)

        flagged = export_flags(results)

        generate_report(securities, results)

        print("\n======================================")
        print("VALIDATION COMPLETED")
        print("======================================")

        print(f"Records checked: {len(securities)}")
        print(f"Records flagged: {len(flagged)}")

        print("\nOriginal CSV preserved.")
        print("Database was not modified.")

    except Exception as error:

        print("\nValidation failed.")
        print(f"Reason: {error}")


if __name__ == "__main__":
    main()
