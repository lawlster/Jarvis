from tools.valuation import calculate_dcf

result = calculate_dcf(
    free_cash_flow=10_000_000_000,
    growth_rate=0.10,
    discount_rate=0.10,
    terminal_growth_rate=0.03,
    years=5,
    net_debt=5_000_000_000,
    shares_outstanding=1_000_000_000
)

print(result)