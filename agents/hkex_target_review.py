
import csv
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

CSV_PATH = (
    BASE_DIR
    / "data"
    / "market_data"
    / "hkex"
    / "classified_securities.csv"
)

FIELDS_TO_DISPLAY = [
    "stock_code",
    "name",
    "category",
    "subcategory",
    "isin",
    "trading_currency",
    "rmb_counter",
    "board_lot",
    "shortsell_eligible",
    "expiry_date",
    "classification",
    "classification_reason",
]


def display_record(row):
    for field in FIELDS_TO_DISPLAY:
        print(f"  {field}: {row.get(field, '')}")


def main():
    with CSV_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        rows = list(csv.DictReader(file))

    print("=" * 55)
    print("HKEX TARGET UNIVERSE - DETAILED REVIEW")
    print("=" * 55)

    target_rows = [
        row for row in rows
        if row["classification"] in (
            "include_candidate",
            "manual_review",
        )
    ]

    missing_isin = [
        row for row in target_rows
        if not row["isin"].strip()
    ]

    print(f"\nMISSING ISIN RECORDS: {len(missing_isin)}")

    for row in missing_isin:
        print("\n" + "-" * 45)
        display_record(row)

    isin_groups = defaultdict(list)

    for row in rows:
        isin = row["isin"].strip()

        if isin:
            isin_groups[isin].append(row)

    duplicate_groups = {
        isin: records
        for isin, records in isin_groups.items()
        if len(records) > 1
        and any(
            row["classification"] == "include_candidate"
            for row in records
        )
    }

    print(
        "\n\nDUPLICATE ISIN GROUPS INVOLVING CANDIDATES:",
        len(duplicate_groups),
    )

    for isin, records in duplicate_groups.items():
        print(f"\nISIN: {isin}")

        for row in records:
            print("\n" + "-" * 35)
            display_record(row)

    print("\n" + "=" * 55)
    print("DETAILED REVIEW COMPLETED")
    print("=" * 55)


if __name__ == "__main__":
    main()

