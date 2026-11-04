
def classify_valuation(valuation):

    if not isinstance(valuation, dict):
        return {"error": "Invalid valuation data."}

    if "error" in valuation:
        return {
            "status": "insufficient_data",
            "error": valuation["error"]
        }

    ticker = valuation.get("ticker")
    current_price = valuation.get("current_price")
    intrinsic_value = valuation.get("estimated_intrinsic_value")
    threshold_price = valuation.get("threshold_price")

    # Validate required information
    if not all(isinstance(value, (int, float)) for value in [
        current_price,
        intrinsic_value,
        threshold_price
    ]):
        return {
            "status": "insufficient_data",
            "error": "Missing or invalid valuation figures."
        }

    if current_price <= 0 or intrinsic_value <= 0:
        return {
            "status": "insufficient_data",
            "error": "Prices and intrinsic value must be positive."
        }

    if threshold_price < 0 or threshold_price > intrinsic_value:
        return {
            "status": "insufficient_data",
            "error": "Invalid margin-of-safety threshold."
        }

    # Calculate difference from estimated intrinsic value
    difference_percent = (
        (intrinsic_value - current_price) / intrinsic_value
    ) * 100

    # Classify valuation
    if current_price <= threshold_price:

        status = "below_threshold"
        explanation = (
            "Market price is at or below the predefined "
            "margin-of-safety threshold."
        )

    elif current_price <= intrinsic_value:

        status = "below_estimated_value"
        explanation = (
            "Market price is below estimated intrinsic value "
            "but above the predefined margin-of-safety threshold."
        )

    else:

        status = "above_estimated_value"
        explanation = (
            "Market price is above the estimated intrinsic value."
        )

    return {
        "ticker": ticker,
        "status": status,
        "explanation": explanation,
        "current_price": current_price,
        "estimated_intrinsic_value": intrinsic_value,
        "threshold_price": threshold_price,
        "difference_from_estimate_percent": difference_percent,
        "valuation_assumptions": valuation.get(
            "valuation_assumptions", {}
        )
    }


def classify_watchlist_valuations(watchlist_report):

    if "error" in watchlist_report:
        return watchlist_report

    results = watchlist_report.get("results", [])

    classifications = []

    summary = {
        "below_threshold": 0,
        "below_estimated_value": 0,
        "above_estimated_value": 0,
        "insufficient_data": 0
    }

    for result in results:

        ticker = result.get("ticker", "UNKNOWN")
        valuation = result.get("valuation", {})

        classification = classify_valuation(valuation)

        if "ticker" not in classification:
            classification["ticker"] = ticker

        status = classification.get(
            "status",
            "insufficient_data"
        )

        if status not in summary:
            status = "insufficient_data"
            classification["status"] = status

        summary[status] += 1

        classifications.append(classification)

    return {
        "total_companies": len(classifications),
        "summary": summary,
        "classifications": classifications
    }
