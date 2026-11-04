
from datetime import datetime, timezone, timedelta


def check_financial_data(data):
    """
    Evaluate the completeness and reliability of
    financial information returned by a provider.

    This is a data-quality check, not an investment score.
    """

    if not isinstance(data, dict):
        return {
            "status": "invalid",
            "quality_score": 0,
            "issues": ["Financial data is not a valid dictionary."]
        }

    if "error" in data:
        return {
            "status": "failed",
            "quality_score": 0,
            "issues": [data["error"]]
        }

    issues = []
    score = 100

    financial_fields = [
        "revenue",
        "operating_income",
        "net_income",
        "free_cash_flow",
        "total_debt",
        "cash"
    ]

    available_fields = 0

    for field in financial_fields:

        value = data.get(field)

        if isinstance(value, dict) and value:
            available_fields += 1
        else:
            issues.append(
                f"Missing financial information: {field}"
            )
            score -= 10

    if available_fields == 0:
        issues.append("No financial statements are available.")

    # Check price availability.
    current_price = data.get("current_price")

    if current_price is None:
        issues.append("Current market price is unavailable.")
        score -= 15

    # Check retrieval timestamp.
    freshness = data.get("data_freshness", {})

    retrieved_at = freshness.get("retrieved_at_utc")

    if not retrieved_at:
        issues.append("Retrieval timestamp is missing.")
        score -= 10

    else:
        try:
            retrieved_time = datetime.fromisoformat(
                retrieved_at.replace("Z", "+00:00")
            )

            age = datetime.now(
                timezone.utc
            ) - retrieved_time

            if age > timedelta(days=7):
                issues.append(
                    "Financial data retrieval is more than 7 days old."
                )
                score -= 15

        except (ValueError, TypeError):
            issues.append("Retrieval timestamp is invalid.")
            score -= 10

    # Check reporting periods.
    periods = freshness.get(
        "financial_statements_latest_period",
        {}
    )

    if not periods:
        issues.append("Financial reporting periods are unavailable.")
        score -= 10

    score = max(0, min(100, score))

    if score >= 85:
        status = "good"

    elif score >= 60:
        status = "warning"

    else:
        status = "poor"

    return {
        "status": status,
        "quality_score": score,
        "available_financial_fields": available_fields,
        "total_financial_fields": len(financial_fields),
        "issues": issues
    }


def assess_provider_response(data):

    quality = check_financial_data(data)

    return {
        "provider": data.get("data_source", "Unknown")
        if isinstance(data, dict)
        else "Unknown",

        "ticker": data.get("ticker")
        if isinstance(data, dict)
        else None,

        "quality_assessment": quality,

        "valuation_eligible": (
            quality["status"] == "good"
        )
    }
