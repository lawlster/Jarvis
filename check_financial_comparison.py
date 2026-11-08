
from agents.financial_tracker import (
    initialise_financial_tracker,
    save_financial_snapshot
)

from agents.financial_comparison import compare_financial_snapshots


# Ensure the database table exists
initialise_financial_tracker()

# Save another snapshot
print(save_financial_snapshot("MSFT"))

# Compare the two latest snapshots
result = compare_financial_snapshots("MSFT")

print("\nMICROSOFT FINANCIAL COMPARISON")
print("=" * 60)

if isinstance(result, str):
    print(result)

else:
    print("Previous snapshot:", result["previous_snapshot"])
    print("Latest snapshot:", result["latest_snapshot"])

    for metric, values in result["metrics"].items():

        print("\nMetric:", metric.upper())

        if "status" in values:
            print("Status:", values["status"])
            continue

        print("Reporting period:", values["reporting_date"])
        print("Previous:", values["previous_value"])
        print("Latest:", values["latest_value"])
        print("Change:", values["absolute_change"])
        print("Percentage change:", values["percentage_change"])
