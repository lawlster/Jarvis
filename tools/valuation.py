
def calculate_dcf(
    free_cash_flow,
    growth_rate,
    discount_rate,
    terminal_growth_rate,
    years=5,
    net_debt=0,
    shares_outstanding=None
):
    """
    Calculate a simplified discounted cash flow valuation.

    Rates must be decimals:
    10% = 0.10

    Net debt = Total debt - Cash.
    """

    try:

        if free_cash_flow <= 0:
            return {"error": "Free cash flow must be positive."}

        if years < 1 or years > 20:
            return {"error": "Forecast period must be 1-20 years."}

        if discount_rate <= terminal_growth_rate:
            return {
                "error": "Discount rate must exceed terminal growth rate."
            }

        if discount_rate <= 0:
            return {"error": "Discount rate must be positive."}

        if terminal_growth_rate < -0.05:
            return {"error": "Terminal growth rate is too low."}

        if shares_outstanding is not None and shares_outstanding <= 0:
            return {"error": "Shares outstanding must be positive."}

        projected_cash_flows = []

        present_value_of_cash_flows = 0

        for year in range(1, years + 1):

            projected_fcf = (
                free_cash_flow * (1 + growth_rate) ** year
            )

            present_value = (
                projected_fcf / (1 + discount_rate) ** year
            )

            projected_cash_flows.append({
                "year": year,
                "projected_fcf": projected_fcf,
                "present_value": present_value
            })

            present_value_of_cash_flows += present_value

        final_fcf = projected_cash_flows[-1]["projected_fcf"]

        terminal_value = (
            final_fcf * (1 + terminal_growth_rate)
            / (discount_rate - terminal_growth_rate)
        )

        present_terminal_value = (
            terminal_value / (1 + discount_rate) ** years
        )

        enterprise_value = (
            present_value_of_cash_flows
            + present_terminal_value
        )

        equity_value = enterprise_value - net_debt

        value_per_share = None

        if shares_outstanding is not None:
            value_per_share = equity_value / shares_outstanding

        return {
            "projected_cash_flows": projected_cash_flows,
            "present_value_of_cash_flows": present_value_of_cash_flows,
            "terminal_value": terminal_value,
            "present_terminal_value": present_terminal_value,
            "enterprise_value": enterprise_value,
            "net_debt": net_debt,
            "equity_value": equity_value,
            "intrinsic_value_per_share": value_per_share
        }

    except Exception as error:

        return {"error": str(error)}
