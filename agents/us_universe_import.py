"""Step 15J — US universe import (workspace module, Phase A close-out).

DRY-RUN BY DEFAULT. This module never writes to the database unless
``--apply`` is passed explicitly.

Important: all 7,625 US listings are ALREADY in ``market_universe``
(all labelled generic ``equity``, sector/industry blank). 15J is
therefore an UPDATE of ``security_type`` on existing rows, not an
insert — no new rows are created and no rows are deleted.

Approved scope (owner decision 2026-11-08, "finish it"):
- Core equity-like rows ONLY: classification_status confirmed or
  provisional AND security_form common_share or adr_ads
  (5,189 rows: 4,874 common + 315 ADR).
- security_type mapping: common_share -> 'common_stock',
  adr_ads -> 'adr'.
- NOT touched: special forms (preferred 118, warrant 358, unit 284,
  rights 103, debt_note 158, ETP 29 = 1,050 rows — 15I review still
  pending) and unknown/conflicting rows (1,372 — manual review).
  Those keep their existing generic 'equity' label, so unclassified
  rows stay identifiable.
- Matched on (ticker, exchange, country='USA'). Any candidate that
  does not match exactly is reported and skipped, never inserted.

Usage:
  python agents/us_universe_import.py            # dry run
  python agents/us_universe_import.py --apply    # real update (approved)
"""

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CSV_PATH = BASE_DIR / "data" / "market_data" / "us_security_classification_v4_2" / "us_security_classification_candidates.csv"
DATABASE_PATH = BASE_DIR / "database" / "assistant.db"

FORM_TO_TYPE = {"common_share": "common_stock", "adr_ads": "adr"}


def load_core():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    core = [r for r in rows
            if r.get("classification_status") in ("confirmed", "provisional")
            and r.get("security_form") in FORM_TO_TYPE]
    rest = [r for r in rows if r not in core]
    return rows, core, rest


def dry_run():
    rows, core, rest = load_core()
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    db = {(t, e) for t, e in conn.execute(
        "SELECT ticker, exchange FROM market_universe WHERE country = 'USA'")}
    conn.close()
    matched = [r for r in core if (r["ticker"], r["exchange"]) in db]
    unmatched = [r for r in core if (r["ticker"], r["exchange"]) not in db]
    print(f"v4_2 candidate rows: {len(rows)}")
    print(f"Core scope (confirmed/provisional + common/ADR): {len(core)}")
    print(f"  common_share: {sum(1 for r in core if r['security_form'] == 'common_share')}")
    print(f"  adr_ads: {sum(1 for r in core if r['security_form'] == 'adr_ads')}")
    print(f"Matched on (ticker, exchange) in DB: {len(matched)}")
    print(f"Unmatched (would be skipped, never inserted): {len(unmatched)}")
    print(f"Not in scope (specials + unknown/conflicting, untouched): {len(rest)}")
    print("Dry run only — no database writes. Pass --apply to update, only with approval.")
    return core


def apply_import():
    rows, core, rest = load_core()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        updated = 0
        already = 0
        unmatched = 0
        now = datetime.now(timezone.utc).isoformat()
        for r in core:
            new_type = FORM_TO_TYPE[r["security_form"]]
            cur = conn.execute(
                "SELECT security_type FROM market_universe "
                "WHERE ticker = ? AND exchange = ? AND country = 'USA'",
                (r["ticker"], r["exchange"]))
            found = cur.fetchone()
            if found is None:
                unmatched += 1
                continue
            if found[0] == new_type:
                already += 1
                continue
            conn.execute(
                "UPDATE market_universe SET security_type = ?, "
                "last_updated = ? "
                "WHERE ticker = ? AND exchange = ? AND country = 'USA'",
                (new_type, now, r["ticker"], r["exchange"]))
            updated += 1
        conn.commit()
        print(f"Updated: {updated}, already correct: {already}, "
              f"unmatched (skipped): {unmatched}")
        print(f"Untouched (specials + unknown/conflicting): {len(rest)}")
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
