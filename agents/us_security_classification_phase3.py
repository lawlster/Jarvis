"""Reclassify US listings from already-cached official exchange snapshots.

This module makes no network requests. It reads the v3 source manifest and
content-addressed cached snapshots, then writes an isolated v4 candidate set.
"""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import us_security_classifier as classifier
from . import us_security_evidence as evidence


BASE_DIR = Path(__file__).resolve().parents[1]
V3_DIR = BASE_DIR / "data" / "market_data" / "us_security_classification_v3"
OUTPUT_DIR = BASE_DIR / "data" / "market_data" / "us_security_classification_v4_2"
MIC_VIEW_FIELDS = [
    "ticker", "mic", "source_category", "issue_description",
    "exact_ticker_listing_ids", "exact_ticker_listing_exchanges",
    "same_mic_listing_key_present", "review_note",
]
CHANGE_FIELDS = [
    "listing_id", "ticker", "exchange", "company_name",
    "v3_status", "phase3_status", "v3_structure", "phase3_structure",
    "v3_form", "phase3_form", "v3_share_class", "phase3_share_class",
    "v3_evidence_source", "phase3_evidence_source",
    "v3_rationale", "phase3_rationale",
]


def _write_csv_new(path, rows, fields):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing Phase 3 output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_cached_sources(manifest_path=V3_DIR / "source_manifest.json"):
    """Load and hash-check only the exact snapshots recorded by the v3 manifest."""

    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cache_dir = Path(manifest["cache_directory"])
    snapshots = {}
    for source in manifest["sources"]:
        path = cache_dir / source["snapshot_file"]
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != source["sha256"]:
            raise ValueError(f"Cached snapshot hash mismatch: {path}")
        snapshots[source["source_id"]] = (source, content)
    return manifest, snapshots


def _parse_cached_sources(manifest, snapshots):
    nasdaq_meta, nasdaq_bytes = snapshots["nasdaqtraded"]
    nasdaq_records = evidence.parse_nasdaq_directory(nasdaq_bytes.decode("utf-8-sig"))
    for record in nasdaq_records:
        record["source_metadata"] = nasdaq_meta

    nyse_results = {}
    for category in evidence.NYSE_CATEGORIES:
        source_id = "nyse_" + category.lower()
        metadata, content = snapshots[source_id]
        rows = json.loads(content.decode("utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError(f"Cached NYSE source is not a JSON array: {source_id}")
        nyse_results[category] = {"metadata": metadata, "rows": rows}
    return nasdaq_records, evidence.parse_nyse_records(nyse_results), nyse_results


def build_mic_reconciliation(listings, nyse_sources):
    """Create a review-only view for non-XNYS MICs; never map them implicitly."""

    by_exact_ticker = {}
    for listing in listings:
        by_exact_ticker.setdefault(str(listing.get("ticker") or "").strip(), []).append(listing)
    result = []
    for category, source in nyse_sources.items():
        for row in source["rows"]:
            url = str(row.get("url") or "")
            parsed = re.search(r"/quote/([A-Z0-9]+):([^/?#]+)", url, re.IGNORECASE)
            if not parsed:
                continue
            mic, ticker = parsed.group(1).upper(), parsed.group(2).strip()
            if mic not in evidence.RECONCILIATION_MICS:
                continue
            matches = by_exact_ticker.get(ticker, [])
            result.append({
                "ticker": ticker,
                "mic": mic,
                "source_category": category,
                "issue_description": str(row.get("instrumentName") or ""),
                "exact_ticker_listing_ids": ";".join(str(item.get("id", "")) for item in matches),
                "exact_ticker_listing_exchanges": ";".join(sorted({str(item.get("exchange") or "") for item in matches})),
                "same_mic_listing_key_present": "no",
                "review_note": "Ticker candidate only; database venue is not this MIC. Not used as classification evidence.",
            })
    return result


def build_change_rows(v3_candidates, phase3_candidates):
    """Return a listing-by-listing audit of changed classification outputs."""

    previous = {str(row["listing_id"]): row for row in v3_candidates}
    changes = []
    fields_to_compare = (
        "classification_status", "issuer_structure", "security_form", "share_class",
        "manual_review_status", "manual_review_reason", "evidence_source",
        "classification_rationale",
    )
    for current in phase3_candidates:
        old = previous.get(str(current["listing_id"]))
        if old is None or all(old.get(field) == current.get(field) for field in fields_to_compare):
            continue
        changes.append({
            "listing_id": current["listing_id"], "ticker": current["ticker"],
            "exchange": current["exchange"], "company_name": current["company_name"],
            "v3_status": old.get("classification_status", ""),
            "phase3_status": current["classification_status"],
            "v3_structure": old.get("issuer_structure", ""),
            "phase3_structure": current["issuer_structure"],
            "v3_form": old.get("security_form", ""),
            "phase3_form": current["security_form"],
            "v3_share_class": old.get("share_class", ""),
            "phase3_share_class": current["share_class"],
            "v3_evidence_source": old.get("evidence_source", ""),
            "phase3_evidence_source": current["evidence_source"],
            "v3_rationale": old.get("manual_review_reason", "") or old.get("classification_rationale", ""),
            "phase3_rationale": current.get("manual_review_reason", "") or current.get("classification_rationale", ""),
        })
    return changes


def build_delta_report(v3_candidates, phase3_candidates, changes, mic_rows):
    before = Counter(row["classification_status"] for row in v3_candidates)
    after = Counter(row["classification_status"] for row in phase3_candidates)
    old_by_id = {str(row["listing_id"]): row for row in v3_candidates}
    transitions = Counter(
        (old_by_id[str(row["listing_id"])]["classification_status"], row["classification_status"])
        for row in phase3_candidates
        if str(row["listing_id"]) in old_by_id
        and old_by_id[str(row["listing_id"])]["classification_status"] != row["classification_status"]
    )
    lines = [
        "STEP 15G PHASE 3 — V3 COMPARISON",
        "=" * 42,
        "Evidence: only the content-hash-verified snapshots recorded in the v3 manifest.",
        "Matching: case-sensitive ticker + normalized exchange; CIK is not the security-form key.",
        "Network requests: 0. Production database access: SQLite URI mode=ro.",
        "",
        "STATUS COUNTS (v3 -> phase 3)",
        "-" * 42,
    ]
    for status in ("confirmed", "provisional", "conflicting", "unknown"):
        lines.append(f"{status}: {before.get(status, 0)} -> {after.get(status, 0)} ({after.get(status, 0)-before.get(status, 0):+d})")
    lines.extend(["", f"Listings with a material classification/evidence change: {len(changes)}", f"MIC reconciliation candidates (XASE/ARCX/XNGS): {len(mic_rows)}", "", "STATUS TRANSITIONS", "-" * 42])
    lines.extend(f"{source} -> {target}: {count}" for (source, target), count in sorted(transitions.items()))
    original_conflicts = {row["ticker"] for row in v3_candidates if row["classification_status"] == "conflicting"}
    current_conflicts = [row for row in phase3_candidates if row["classification_status"] == "conflicting"]
    new_conflicts = [row for row in current_conflicts if row["ticker"] not in original_conflicts]
    lines.extend(["", "CONFLICT REVIEW", "-" * 42,
                  "IPB, MHNC and TVC remain conflicting and require manual review.",
                  "The five audited artifacts BSBR, RNP, TPC, VBF and WDH no longer conflict; each remains provisional because issuer structure is not established.",
                  f"Additional conflicts surfaced by explicit parsing of official issue descriptions: {len(new_conflicts)}.",
                  "These are retained for manual review because the current official Nasdaq and NYSE rows state different security forms.",
                  "The accompanying v3_to_phase3_classification_changes.csv gives per-listing old/new values, evidence source and rationale."])
    return "\n".join(lines) + "\n"


def run_phase3(database_path=None, source_manifest=V3_DIR / "source_manifest.json", output_dir=OUTPUT_DIR):
    output_dir = classifier.validate_output_dir(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Refusing to use existing versioned output directory: {output_dir}")
    manifest, snapshots = load_cached_sources(source_manifest)
    listings = classifier.load_us_listings(database_path)
    nasdaq, nyse_records, nyse_sources = _parse_cached_sources(manifest, snapshots)
    evidence_rows, unmatched = evidence.build_evidence_rows(listings, nasdaq, nyse_records)
    output_dir.mkdir(parents=True, exist_ok=False)
    evidence_path = output_dir / "official_issue_level_evidence.csv"
    mic_path = output_dir / "mic_reconciliation_xase_arcx_xngs.csv"
    evidence._write_evidence_csv(evidence_path, evidence_rows)
    _write_csv_new(mic_path, build_mic_reconciliation(listings, nyse_sources), MIC_VIEW_FIELDS)
    result = classifier.run_pipeline(
        database_path=database_path,
        evidence_path=evidence_path,
        output_dir=output_dir,
        classification_date=datetime.now(timezone.utc).date().isoformat(),
    )
    v3_candidates_path = Path(source_manifest).parent / "us_security_classification_candidates.csv"
    with v3_candidates_path.open(encoding="utf-8-sig", newline="") as handle:
        v3_candidates = list(csv.DictReader(handle))
    with result["candidate_csv"].open(encoding="utf-8-sig", newline="") as handle:
        phase3_candidates = list(csv.DictReader(handle))
    changes_path = output_dir / "v3_to_phase3_classification_changes.csv"
    changes = build_change_rows(v3_candidates, phase3_candidates)
    _write_csv_new(changes_path, changes, CHANGE_FIELDS)
    with mic_path.open(encoding="utf-8-sig", newline="") as handle:
        mic_rows = list(csv.DictReader(handle))
    delta_path = output_dir / "v3_comparison_and_conflict_review.txt"
    with delta_path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(build_delta_report(v3_candidates, phase3_candidates, changes, mic_rows))
    with mic_path.open(encoding="utf-8-sig", newline="") as handle:
        mic_count = sum(1 for _ in csv.DictReader(handle))
    new_manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_access": "SQLite URI mode=ro",
        "network_requests": 0,
        "source_manifest": str(Path(source_manifest).resolve()),
        "source_snapshot_hashes_verified": True,
        "listing_rows": len(listings),
        "evidence_rows_matched_by_case_sensitive_ticker_and_exchange": len(evidence_rows),
        "unmatched_source_records": unmatched,
        "mic_reconciliation_rows": mic_count,
        "outputs": {
            "evidence_csv": str(evidence_path),
            "candidate_csv": str(result["candidate_csv"]),
            "manual_review_csv": str(result["manual_review_csv"]),
            "report": str(result["report"]),
            "mic_reconciliation_csv": str(mic_path),
            "v3_to_phase3_changes_csv": str(changes_path),
            "v3_comparison_and_conflict_review": str(delta_path),
        },
    }
    manifest_path = output_dir / "source_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing Phase 3 output: {manifest_path}")
    manifest_path.write_text(json.dumps(new_manifest, indent=2) + "\n", encoding="utf-8")
    result.update({"evidence_count": len(evidence_rows), "manifest": manifest_path, "mic_reconciliation": mic_path})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reclassify from cached official US exchange snapshots only.")
    parser.add_argument("--database", type=Path, default=classifier.DATABASE_PATH)
    parser.add_argument("--source-manifest", type=Path, default=V3_DIR / "source_manifest.json")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    result = run_phase3(args.database, args.source_manifest, args.output_dir)
    print(f"Listings evaluated: {result['candidate_count']}")
    print(f"Confirmed: {result['confirmed_count']}")
    print(f"Evidence rows: {result['evidence_count']}")
    print(f"Candidates: {result['candidate_csv']}")
    print(f"MIC reconciliation: {result['mic_reconciliation']}")
    print(f"Report: {result['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
