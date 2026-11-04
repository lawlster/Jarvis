
from agents.valuation_history import get_valuation_history
from agents.watchlist import get_watchlist


def percentage_change(old_value, new_value):
    """Calculate percentage change safely."""
    if old_value is None or new_value is None:
        return None

    if old_value == 0:
        return None

    return ((new_value - old_value) / abs(old_value)) * 100


def get_price_vs_intrinsic(current_price, intrinsic_value):
    """Calculate how far the market price is from estimated intrinsic value."""
    if current_price is None or intrinsic_value is None:
        return None

    if intrinsic_value <= 0:
        return None

    return ((intrinsic_value - current_price) / intrinsic_value) * 100


def analyse_valuation_trends(ticker):
    ticker = ticker.upper().strip()

    history = get_valuation_history(ticker)

    if not history:
        return {
            "ticker": ticker,
            "status": "no_history",
            "message": "No historical valuation snapshots found."
        }

    if len(history) < 2:
        latest = history[-1]

        return {
            "ticker": ticker,
            "status": "insufficient_history",
            "message": "At least two valuation snapshots are needed to compare changes.",
            "snapshot_count": 1,
            "latest_snapshot": latest
        }

    first = history[0]
    latest = history[-1]

    metrics = [
        "current_price",
        "intrinsic_value",
        "threshold_price"
    ]

    changes = {}

    for metric in metrics:
        old_value = first.get(metric)
        new_value = latest.get(metric)

        changes[metric] = {
            "previous": old_value,
            "latest": new_value,
            "absolute_change": (
                new_value - old_value
                if old_value is not None and new_value is not None
                else None
            ),
            "percentage_change": percentage_change(
                old_value,
                new_value
            )
        }

    previous_gap = get_price_vs_intrinsic(
        first.get("current_price"),
        first.get("intrinsic_value")
    )

    latest_gap = get_price_vs_intrinsic(
        latest.get("current_price"),
        latest.get("intrinsic_value")
    )

    gap_change = (
        latest_gap - previous_gap
        if previous_gap is not None and latest_gap is not None
        else None
    )

    return {
        "ticker": ticker,
        "status": "analysis_available",
        "snapshot_count": len(history),
        "first_snapshot_date": first.get("snapshot_date"),
        "latest_snapshot_date": latest.get("snapshot_date"),
        "latest_classification": latest.get("classification"),
        "changes": changes,
        "price_vs_intrinsic": {
            "previous_percentage": previous_gap,
            "latest_percentage": latest_gap,
            "change_in_percentage_points": gap_change
        },
        "interpretation": {
            "market_price": "Shows how the share price changed between the first and latest saved snapshots.",
            "intrinsic_value": "Shows how the DCF estimate changed between snapshots.",
            "threshold_price": "Shows how the predefined margin-of-safety threshold changed.",
            "price_vs_intrinsic": "A positive percentage means the market price is below the estimated intrinsic value. A negative percentage means it is above."
        }
    }


def analyse_all_valuation_trends():
    companies = get_watchlist()

    if not companies:
        return {"error": "Your watchlist is empty."}

    results = []

    for company in companies:
        ticker = company["ticker"]

        try:
            result = analyse_valuation_trends(ticker)
        except Exception as error:
            result = {
                "ticker": ticker,
                "status": "error",
                "message": str(error)
            }

        results.append(result)

    summary = {
        "total_companies": len(results),
        "analysis_available": sum(
            1 for result in results
            if result.get("status") == "analysis_available"
        ),
        "insufficient_history": sum(
            1 for result in results
            if result.get("status") == "insufficient_history"
        ),
        "no_history": sum(
            1 for result in results
            if result.get("status") == "no_history"
        ),
        "errors": sum(
            1 for result in results
            if result.get("status") == "error"
        )
    }

    return {
        "summary": summary,
        "results": results
    }
