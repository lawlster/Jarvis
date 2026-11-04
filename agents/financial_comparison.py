
from agents.financial_tracker import get_financial_history


METRICS = [
    "revenue",
    "operating_income",
    "net_income",
    "free_cash_flow",
    "total_debt",
    "cash"
]


def compare_financial_snapshots(ticker):

    history = get_financial_history(ticker)

    if len(history) < 2:
        return (
            f"Not enough snapshots for {ticker}. "
            "At least two snapshots are required."
        )

    previous = history[-2]["financial_data"]
    latest = history[-1]["financial_data"]

    comparison = {
        "ticker": ticker.upper(),
        "previous_snapshot": history[-2]["snapshot_date"],
        "latest_snapshot": history[-1]["snapshot_date"],
        "metrics": {}
    }

    for metric in METRICS:

        previous_values = previous.get(metric, {})
        latest_values = latest.get(metric, {})

        # Find reporting periods available in both snapshots
        common_dates = sorted(
            set(previous_values) & set(latest_values),
            reverse=True
        )

        if not common_dates:
            comparison["metrics"][metric] = {
                "status": "No common reporting period"
            }
            continue

        reporting_date = common_dates[0]

        old_value = previous_values[reporting_date]
        new_value = latest_values[reporting_date]

        if old_value is None or new_value is None:
            comparison["metrics"][metric] = {
                "status": "Missing financial value",
                "reporting_date": reporting_date
            }
            continue

        change = new_value - old_value

        percentage_change = None

        if old_value != 0:
            percentage_change = (change / abs(old_value)) * 100

        comparison["metrics"][metric] = {
            "reporting_date": reporting_date,
            "previous_value": old_value,
            "latest_value": new_value,
            "absolute_change": change,
            "percentage_change": percentage_change
        }

    return comparison
