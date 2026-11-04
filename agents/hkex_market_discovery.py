
import requests

from pathlib import Path
from openpyxl import load_workbook


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent

CACHE_DIR = (
    BASE_DIR
    / "data"
    / "market_data"
    / "hkex"
)

WORKBOOK_PATH = CACHE_DIR / "ListOfSecurities.xlsx"

HKEX_LIST_URL = (
    "https://www.hkex.com.hk/"
    "eng/services/trading/securities/"
    "securitieslists/ListOfSecurities.xlsx"
)

REQUEST_TIMEOUT = 45

HEADERS = {
    "User-Agent": (
        "AiAssistant Financial Research System"
    ),
    "Accept": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet,application/octet-stream,*/*"
    )
}


# ==================================================
# DOWNLOAD OFFICIAL HKEX LIST
# ==================================================

def download_hkex_listing_workbook():

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    print("\nDownloading official HKEX securities list...")

    response = requests.get(
        HKEX_LIST_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    # Excel XLSX files are ZIP containers.
    if not response.content.startswith(b"PK"):

        preview = response.text[:300]

        raise RuntimeError(
            "HKEX did not return a valid Excel workbook.\n"
            f"Response content type: "
            f"{response.headers.get('Content-Type')}\n"
            f"Response preview: {preview}"
        )

    WORKBOOK_PATH.write_bytes(
        response.content
    )

    print("Download successful.")
    print(f"Saved to: {WORKBOOK_PATH}")
    print(
        f"File size: "
        f"{len(response.content) / 1024:.2f} KB"
    )

    return WORKBOOK_PATH


# ==================================================
# WORKBOOK INSPECTION
# ==================================================

def inspect_hkex_workbook():

    if not WORKBOOK_PATH.exists():

        raise FileNotFoundError(
            "HKEX workbook not found. "
            "Download it first."
        )

    print("\nOpening HKEX workbook...")

    workbook = load_workbook(
        WORKBOOK_PATH,
        read_only=True,
        data_only=True
    )

    print("\n======================================")
    print("HKEX WORKBOOK STRUCTURE")
    print("======================================")

    print(
        f"\nWorksheets found: "
        f"{len(workbook.sheetnames)}"
    )

    for sheet_name in workbook.sheetnames:

        worksheet = workbook[sheet_name]

        print("\n--------------------------------------")
        print(f"WORKSHEET: {sheet_name}")
        print("--------------------------------------")

        print(f"Rows: {worksheet.max_row}")
        print(f"Columns: {worksheet.max_column}")

        print("\nFirst 12 rows:")

        max_rows = min(
            worksheet.max_row or 0,
            12
        )

        max_columns = min(
            worksheet.max_column or 0,
            20
        )

        for row in worksheet.iter_rows(
            min_row=1,
            max_row=max_rows,
            max_col=max_columns,
            values_only=True
        ):

            values = [
                str(value).strip()
                if value is not None
                else ""
                for value in row
            ]

            while values and not values[-1]:
                values.pop()

            if values:
                print(" | ".join(values))

    workbook.close()


# ==================================================
# MAIN
# ==================================================

def main():

    print("======================================")
    print("AiAssistant - HKEX Market Discovery")
    print("======================================")

    try:

        download_hkex_listing_workbook()

        inspect_hkex_workbook()

        print("\n======================================")
        print("DISCOVERY INSPECTION COMPLETED")
        print("======================================")

        print(
            "\nNo companies have been imported."
        )

        print(
            "No existing database records were modified."
        )

    except Exception as error:

        print("\nHKEX discovery failed.")
        print(f"Reason: {error}")


if __name__ == "__main__":

    main()
