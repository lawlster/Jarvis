
from agents.valuation_monitor import monitor_company_valuation

result = monitor_company_valuation(
    ticker="MSFT",
    growth_rate=0.08,
    discount_rate=0.10,
    terminal_growth_rate=0.025,
    margin_of_safety=0.25,
    years=5
)

print("\nAiAssistant VALUATION MONITOR")
print("=" * 50)

if "error" in result:
    print(result["error"])

else:
    print("Company:", result["ticker"])
    print("Current price:", result["current_price"])
    print("Estimated intrinsic value:", result["estimated_intrinsic_value"])
    print("25% margin-of-safety threshold:", result["threshold_price"])
    print("Difference from estimate (%):", result["difference_from_estimate_percent"])

    print("\nValuation assumptions:")
    for key, value in result["valuation_assumptions"].items():
        print(key, ":", value)
