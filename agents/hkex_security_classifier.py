
import csv

from pathlib import Path
from collections import Counter
from openpyxl import load_workbook


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data" / "market_data" / "hkex"

WORKBOOK_PATH = DATA_DIR / "ListOfSecurities.xlsx"
OUTPUT_PATH = DATA_DIR / "classified_securities.csv"

PREVIEW_LIMIT = 15
FIRST_DATA_ROW = 4


# ==================================================
# CLASSIFICATION RULES
# ==================================================

EXCLUDE_CATEGORIES = {
    "Derivative Warrants",
    "Callable Bull/Bear Contracts",
    "Debt Securities",
    "Exchange Traded Products",
    "Equity Warrants (Main Board)",
    "Equity Warrants (GEM)",
}

MANUAL_REVIEW_SUBCATEGORIES = {
    "Investment Companies",
    "Trading Only Securities",
    "Depositary Receipts",
}


def classify_security(category, subcategory):

    category = str(category or "").strip()
    subcategory = str(subcategory or "").strip()

    if category == "Real Estate Investment Trusts":
        return "include", "reit"

    if category in EXCLUDE_CATEGORIES:
        return "exclude", "non_target_instrument"

    if category == "Equity":

        if subcategory in MANUAL_REVIEW_SUBCATEGORIES:
            return "manual_review", "special_equity_instrument"

        if subcategory in {
            "Equity Securities (Main Board)",
            "Equity Securities (GEM)",
        }:
            return "include_candidate", "equity"

        return "manual_review", "unknown_equity_subcategory"

    return "manual_review", "unknown_category"


# ==================================================
# LOAD SECURITIES
# ==================================================

def load_securities():

    if not WORKBOOK_PATH.exists():
        raise FileNotFoundError(
            f"HKEX workbook not found: {WORKBOOK_PATH}"
        )

    print("\nOpening HKEX workbook...")

    workbook = load_workbook(
        WORKBOOK_PATH,
        read_only=False,
        data_only=True
    )

    worksheet = workbook["ListOfSecurities"]

    securities = []

    for row_number in range(
        FIRST_DATA_ROW,
        worksheet.max_row + 1
    ):

        stock_code = worksheet.cell(row_number, 1).value

        if stock_code is None:
            continue

        stock_code = str(stock_code).strip()

        if not stock_code.isdigit():
            continue

        name = worksheet.cell(row_number, 2).value
        category = worksheet.cell(row_number, 3).value
        subcategory = worksheet.cell(row_number, 4).value

        classification, reason = classify_security(
            category,
            subcategory
        )

        security = {
            "stock_code": stock_code.zfill(5),
            "name": str(name or "").strip(),
            "category": str(category or "").strip(),
            "subcategory": str(subcategory or "").strip(),
            "board_lot": worksheet.cell(row_number, 5).value,
            "isin": str(
                worksheet.cell(row_number, 6).value or ""
            ).strip(),
            "expiry_date": worksheet.cell(row_number, 7).value,
            "shortsell_eligible": worksheet.cell(row_number, 9).value,
            "trading_currency": worksheet.cell(row_number, 17).value,
            "rmb_counter": worksheet.cell(row_number, 18).value,
            "classification": classification,
            "classification_reason": reason,
        }

        securities.append(security)

    workbook.close()

    return securities


# ==================================================
# EXPORT CSV
# ==================================================

def export_classified_securities(securities):

    if not securities:
        raise RuntimeError("No securities available for export.")

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT_PATH,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=list(securities[0].keys())
        )

        writer.writeheader()
        writer.writerows(securities)

    print(f"\nCSV exported to: {OUTPUT_PATH}")


# ==================================================
# REPORT
# ==================================================

def print_report(securities):

    classifications = Counter(
        item["classification"] for item in securities
    )

    categories = Counter(
        item["category"] for item in securities
    )

    subcategories = Counter(
        item["subcategory"] for item in securities
    )

    print("\n======================================")
    print("HKEX CLASSIFICATION REPORT")
    print("======================================")

    print(f"\nTotal securities: {len(securities)}")

    print("\nCLASSIFICATION TOTALS")

    for name, count in classifications.most_common():
        print(f"{name}: {count}")

    print("\nCATEGORY TOTALS")

    for name, count in categories.most_common():
        print(f"{name}: {count}")

    print("\nSUBCATEGORY TOTALS")

    for name, count in subcategories.most_common():
        print(f"{name}: {count}")

    print("\nINCLUDED CANDIDATES — SAMPLE")

    count = 0

    for item in securities:

        if item["classification"] not in {
            "include",
            "include_candidate",
        }:
            continue

        print(
            f'{item["stock_code"]} | '
            f'{item["name"]} | '
            f'{item["category"]} | '
            f'{item["subcategory"]} | '
            f'{item["trading_currency"]}'
        )

        count += 1

        if count >= PREVIEW_LIMIT:
            break

    print("\nMANUAL REVIEW — SAMPLE")

    count = 0

    for item in securities:

        if item["classification"] != "manual_review":
            continue

        print(
            f'{item["stock_code"]} | '
            f'{item["name"]} | '
            f'{item["category"]} | '
            f'{item["subcategory"]}'
        )

        count += 1

        if count >= PREVIEW_LIMIT:
            break


# ==================================================
# MAIN
# ==================================================

def main():

    print("======================================")
    print("AiAssistant - HKEX Security Classifier")
    print("======================================")

    try:

        securities = load_securities()

        # Prevent incomplete classification from being accepted.
        if len(securities) < 1000:
            raise RuntimeError(
                f"Only {len(securities)} securities loaded. "
                "Expected substantially more. CSV not exported."
            )

        print_report(securities)

        export_classified_securities(securities)

        print("\n======================================")
        print("CLASSIFICATION COMPLETED")
        print("======================================")

        print("Original workbook preserved.")
        print("Database was not modified.")

    except Exception as error:

        print("\nClassification failed.")
        print(f"Reason: {error}")


if __name__ == "__main__":
    main()
