
from tools.finance import get_company_financials
from tools.valuation import calculate_dcf
from agents.valuation_profiles import get_valuation_profile


def monitor_company_valuation(
    ticker,
    growth_rate=None,
    discount_rate=None,
    terminal_growth_rate=None,
    margin_of_safety=None,
    years=None
):
    ticker = ticker.upper().strip()

    print(f"\nMonitoring valuation for {ticker}...")

    # Retrieve the company's saved valuation profile
    profile = get_valuation_profile(ticker)

    if profile is None:
        return {
            "error": (
                f"No valuation profile found for {ticker}. "
                "Please create a valuation profile first."
            )
        }

    # Use saved assumptions unless manually overridden
    growth_rate = (
        growth_rate if growth_rate is not None
        else profile["growth_rate"]
    )

    discount_rate = (
        discount_rate if discount_rate is not None
        else profile["discount_rate"]
    )

    terminal_growth_rate = (
        terminal_growth_rate if terminal_growth_rate is not None
        else profile["terminal_growth_rate"]
    )

    margin_of_safety = (
        margin_of_safety if margin_of_safety is not None
        else profile["margin_of_safety"]
    )

    years = (
        years if years is not None
        else profile["forecast_years"]
    )

    # Validate assumptions
    if discount_rate <= terminal_growth_rate:
        return {
            "error": "Discount rate must exceed terminal growth rate."
        }

    if not 0 <= margin_of_safety < 1:
        return {
            "error": "Margin of safety must be between 0 and 1."
        }

    if years < 1 or years > 20:
        return {
            "error": "Forecast period must be between 1 and 20 years."
        }

    # Retrieve company financial information
    financials = get_company_financials(ticker)

    if "error" in financials:
        return financials

    # Retrieve latest available free cash flow
    fcf_history = financials.get("free_cash_flow", {})

    valid_fcf = [
        value for value in fcf_history.values()
        if isinstance(value, (int, float))
    ]

    if not valid_fcf:
        return {
            "error": "No valid free cash flow data available."
        }

    latest_fcf = valid_fcf[0]

    # Retrieve debt and cash
    debt_history = financials.get("total_debt", {})
    cash_history = financials.get("cash", {})

    debt_values = [
        value for value in debt_history.values()
        if isinstance(value, (int, float))
    ]

    cash_values = [
        value for value in cash_history.values()
        if isinstance(value, (int, float))
    ]

    total_debt = debt_values[0] if debt_values else 0
    cash = cash_values[0] if cash_values else 0

    net_debt = total_debt - cash

    # Retrieve current market price and shares outstanding
    current_price = financials.get("current_price")

    if current_price is None:
        return {
            "error": "Current market price unavailable."
        }

    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info
        shares = info.get("sharesOutstanding")

    except Exception as error:
        return {
            "error": f"Unable to retrieve shares outstanding: {error}"
        }

    if not shares:
        return {
            "error": "Shares outstanding unavailable."
        }

    # Calculate DCF valuation
    valuation = calculate_dcf(
        free_cash_flow=latest_fcf,
        growth_rate=growth_rate,
        discount_rate=discount_rate,
        terminal_growth_rate=terminal_growth_rate,
        years=years,
        net_debt=net_debt,
        shares_outstanding=shares
    )

    if "error" in valuation:
        return valuation

    intrinsic_value = valuation["intrinsic_value_per_share"]

    if intrinsic_value is None or intrinsic_value <= 0:
        return {
            "error": "Calculated intrinsic value is invalid."
        }

    # Calculate margin-of-safety threshold
    target_price = intrinsic_value * (1 - margin_of_safety)

    difference = (
        (intrinsic_value - current_price) / intrinsic_value
    ) * 100

    # Return complete valuation results
    return {
        "ticker": ticker,
        "company_name": financials.get("company_name"),
        "current_price": current_price,
        "estimated_intrinsic_value": intrinsic_value,
        "margin_of_safety": margin_of_safety,
        "threshold_price": target_price,
        "difference_from_estimate_percent": difference,
        "latest_free_cash_flow": latest_fcf,
        "net_debt": net_debt,

        "valuation_assumptions": {
            "growth_rate": growth_rate,
            "discount_rate": discount_rate,
            "terminal_growth_rate": terminal_growth_rate,
            "forecast_years": years
        },

        "profile_updated_at": profile["updated_at"],
        "assumptions_source": (
            "Saved valuation profile with any supplied overrides"
        )
    }
