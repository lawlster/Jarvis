"""Step 14I — HKEX import (workspace draft, Phase A).

DRY-RUN BY DEFAULT. This script never writes to the database unless
``--apply`` is passed explicitly, and even then it only inserts rows
whose classification is ``include`` or ``include_candidate``.

It does not import manual_review (29 rows) or exclude (14,487 rows).
It does not overwrite existing market_universe rows.

Usage:
  python agents/hkex_import.py            # dry run: counts + samples
  python agents/hkex_import.py --apply    # real import (needs approval)
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CSV_PATH = BASE_DIR / "data" / "market_data" / "hkex" / "classified_securities.csv"
DATABASE_PATH = BASE_DIR / "database" / "assistant.db"

# Country value is a decision, not a guess: the DB today only has USA
# and Singapore. Default to 'Hong Kong' for the dry run, and surface it
# in the output so Vernon can confirm or change it before --apply.
COUNTRY = "Hong Kong"


def load_candidates():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    candidates = [r for r in rows
                  if r.get("classification") in ("include", "include_candidate")]
    return rows, candidates


def dry_run():
    rows, candidates = load_candidates()
    print(f"Classified HKEX rows: {len(rows)}")
    print(f"Import candidates (include/include_candidate): {len(candidates)}")
    print(f"Manual review (not imported): "
          f"{sum(1 for r in rows if r['classification'] == 'manual_review')}")
    print(f"Excluded (not imported): "
          f"{sum(1 for r in rows if r['classification'] == 'exclude')}")
    print(f"Proposed country value: {COUNTRY}")
    print("Sample candidates:")
    for r in candidates[:5]:
        print(f"  {r['stock_code']} {r['name']} "
              f"({r['classification']}, {r['classification_reason']}, "
              f"{r['trading_currency']}, ISIN {r['isin']})")
    # Collision check: do any HK stock codes already exist as tickers?
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    existing = {row[0] for row in conn.execute(
        "SELECT ticker FROM market_universe")}
    conn.close()
    collisions = [r for r in candidates if r["stock_code"] in existing]
    print(f"Ticker collisions with existing DB rows: {len(collisions)}")
    for r in collisions[:10]:
        print(f"  collision: {r['stock_code']} {r['name']}")
    return candidates


def apply_import():
    rows, candidates = load_candidates()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        inserted = 0
        skipped = 0
        for r in candidates:
            cur = conn.execute(
                "SELECT COUNT(*) FROM market_universe "
                "WHERE ticker = ? AND country = ?",
                (r["stock_code"], COUNTRY))
            if cur.fetchone()[0]:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO market_universe "
                "(ticker, company_name, exchange, country, currency, "
                "sector, industry, security_type, data_source, sec_cik) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["stock_code"], r["name"], "HKEX", COUNTRY,
                 r["trading_currency"], None, None,
                 "reit" if r["classification"] == "include" else "equity",
                 "hkex_classified_securities", None))
            inserted += 1
        conn.commit()
        print(f"Inserted: {inserted}, skipped (already present): {skipped}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to the database")
    args = parser.parse_args()
    if args.apply:
        apply_import()
    else:
        dry_run()
        print("\nDry run only — no database writes. "
              "Pass --apply to import, only with approval.")
