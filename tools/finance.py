
from data.providers.provider_manager import provider_manager


def get_company_financials(ticker, provider_name="yahoo"):
    """
    Retrieve company financial information through
    the central provider manager.

    Maintains the original response structure so existing
    investment modules remain compatible.
    """

    ticker = ticker.upper().strip()

    try:

        profile = provider_manager.get_company_profile(
            ticker,
            provider_name
        )

        result = provider_manager.get_company_financials(
            ticker,
            provider_name
        )

        if isinstance(profile, dict) and "error" in profile:
            profile = {}

        if isinstance(result, dict) and "error" in result:
            return {
                "error": result["error"],
                "ticker": ticker
            }

        financials = result.get("financials", {})

        reporting_periods = result.get(
            "financial_statements_latest_period",
            {}
        )

        return {

            "ticker": ticker,

            "company_name": profile.get("company_name"),

            "sector": profile.get("sector"),

            "industry": profile.get("industry"),

            "exchange": profile.get("exchange"),

            "country": profile.get("country"),

            "currency": profile.get("currency"),

            "data_source": result.get(
                "provider",
                provider_name
            ),

            "data_available": result.get(
                "data_available",
                False
            ),

            "data_freshness": {

                "retrieved_at_utc": result.get(
                    "retrieved_at_utc"
                ),

                "price_data_as_of_utc": result.get(
                    "price_data_as_of_utc"
                ),

                "financial_statements_latest_period": (
                    reporting_periods
                )
            },

            "market_cap": financials.get("market_cap"),

            "current_price": financials.get("current_price"),

            "trailing_pe": financials.get("trailing_pe"),

            "forward_pe": financials.get("forward_pe"),

            "revenue": financials.get("revenue", {}),

            "operating_income": financials.get(
                "operating_income",
                {}
            ),

            "net_income": financials.get(
                "net_income",
                {}
            ),

            "free_cash_flow": financials.get(
                "free_cash_flow",
                {}
            ),

            "total_debt": financials.get(
                "total_debt",
                {}
            ),

            "cash": financials.get("cash", {}),

            "shares_outstanding": financials.get(
                "shares_outstanding"
            )
        }

    except Exception as error:

        return {
            "error": str(error),
            "ticker": ticker
        }
