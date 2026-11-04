
from agents.financial_tracker import (
    initialise_financial_tracker,
    save_financial_snapshot,
    get_financial_history
)

# Create database table
initialise_financial_tracker()

# Save a financial snapshot
print(save_financial_snapshot("MSFT"))

# Retrieve historical snapshots
history = get_financial_history("MSFT")

print("\nMICROSOFT FINANCIAL HISTORY")
print("=" * 50)

for snapshot in history:

    data = snapshot["financial_data"]

    print("\nSnapshot date:", snapshot["snapshot_date"])
    print("Company:", data.get("company_name"))
    print("Revenue:", data.get("revenue"))
    print("Free cash flow:", data.get("free_cash_flow"))
    print("Net income:", data.get("net_income"))
