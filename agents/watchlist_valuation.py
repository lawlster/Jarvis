
from agents.watchlist import get_watchlist
from agents.valuation_monitor import monitor_company_valuation
from agents.valuation_classifier import classify_watchlist_valuations

from agents.valuation_history import (
    initialise_valuation_history,
    save_watchlist_valuation_history
)


def evaluate_watchlist(db_path=None):
    # Step 52: ``db_path`` is passed through to the valuation
    # monitor so the profile read can point at a temporary
    # database; the default is unchanged.

    # Ensure historical valuation storage exists
    initialise_valuation_history()

    companies = get_watchlist()

    if not companies:
        return {
            "error": "Your watchlist is empty."
        }

    results = []

    print("\n" + "=" * 65)
    print("AiAssistant - AUTOMATED WATCHLIST VALUATION")
    print("=" * 65)

    print(f"\nFound {len(companies)} companies.\n")

    for index, company in enumerate(companies, start=1):

        ticker = company["ticker"]

        print(
            f"[{index}/{len(companies)}] "
            f"Evaluating {ticker}..."
        )

        try:
            valuation = monitor_company_valuation(
                ticker, db_path=db_path)

            results.append({
                "ticker": ticker,
                "valuation": valuation
            })

        except Exception as error:

            results.append({
                "ticker": ticker,
                "valuation": {
                    "error": str(error)
                }
            })

    successful = sum(
        1 for result in results
        if "error" not in result["valuation"]
    )

    failed = len(results) - successful

    report = {
        "total_companies": len(companies),
        "successful_valuations": successful,
        "failed_valuations": failed,
        "results": results
    }

    # Classify completed valuations
    classification_report = classify_watchlist_valuations(report)

    report["classification_summary"] = (
        classification_report.get("summary", {})
    )

    report["classifications"] = (
        classification_report.get("classifications", [])
    )

    # Save every valuation run to historical storage
    history_result = save_watchlist_valuation_history(report)

    report["history_storage"] = history_result

    print("\n" + "=" * 65)
    print("WATCHLIST VALUATION COMPLETED")
    print("=" * 65)

    print("Total companies:", len(companies))
    print("Successful valuations:", successful)
    print("Failed valuations:", failed)

    print("\nVALUATION CLASSIFICATIONS")

    for item in report["classifications"]:

        print(
            item["ticker"],
            ":",
            item["status"]
        )

    print("\nHISTORICAL RECORDS")
    print("Saved:", history_result["saved"])
    print("Failed:", history_result["failed"])

    return report
