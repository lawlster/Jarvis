
from data.providers.yahoo_provider import YahooFinanceProvider

from data.providers.data_quality import (
    assess_provider_response
)

from data.providers.provider_logger import (
    log_provider_request
)


class ProviderManager:

    def __init__(self):

        self.providers = {}

        self.provider_priority = []

        self.register_provider(
            "yahoo",
            YahooFinanceProvider()
        )

    def register_provider(self, name, provider):

        name = name.lower()

        self.providers[name] = provider

        if name not in self.provider_priority:
            self.provider_priority.append(name)

    def get_provider(self, name="yahoo"):

        name = name.lower()

        provider = self.providers.get(name)

        if provider is None:
            return {
                "error": f"Provider '{name}' is not registered."
            }

        return provider

    def get_company_profile(
        self,
        ticker,
        provider_name="yahoo"
    ):

        provider = self.get_provider(provider_name)

        if isinstance(provider, dict):
            return provider

        return provider.get_company_profile(ticker)

    def convert_to_quality_format(self, result):

        financials = result.get("financials", {})

        return {
            "ticker": result.get("ticker"),

            "data_source": result.get("provider"),

            "data_freshness": {
                "retrieved_at_utc": result.get(
                    "retrieved_at_utc"
                ),

                "financial_statements_latest_period": result.get(
                    "financial_statements_latest_period",
                    {}
                )
            },

            "market_cap": financials.get("market_cap"),

            "current_price": financials.get("current_price"),

            "revenue": financials.get("revenue", {}),

            "operating_income": financials.get(
                "operating_income", {}
            ),

            "net_income": financials.get(
                "net_income", {}
            ),

            "free_cash_flow": financials.get(
                "free_cash_flow", {}
            ),

            "total_debt": financials.get(
                "total_debt", {}
            ),

            "cash": financials.get("cash", {})
        }

    def assess_data_quality(self, result):

        return assess_provider_response(result)

    def get_company_financials(
        self,
        ticker,
        provider_name="yahoo"
    ):

        return self.get_company_financials_with_fallback(
            ticker,
            preferred_provider=provider_name
        )

    def get_company_financials_with_fallback(
        self,
        ticker,
        preferred_provider=None
    ):

        ticker = ticker.upper().strip()

        attempted = []

        provider_order = []

        if preferred_provider:
            provider_order.append(
                preferred_provider.lower()
            )

        provider_order.extend(
            name for name in self.provider_priority
            if name not in provider_order
        )

        for provider_name in provider_order:

            provider = self.get_provider(provider_name)

            if isinstance(provider, dict):

                error_message = provider["error"]

                attempted.append({
                    "provider": provider_name,
                    "error": error_message
                })

                log_provider_request(
                    ticker=ticker,
                    provider=provider_name,
                    status="failed",
                    error_message=error_message
                )

                continue

            try:

                result = provider.get_company_financials(
                    ticker
                )

                if not isinstance(result, dict):

                    error_message = "Invalid provider response."

                    attempted.append({
                        "provider": provider_name,
                        "error": error_message
                    })

                    log_provider_request(
                        ticker=ticker,
                        provider=provider_name,
                        status="failed",
                        error_message=error_message
                    )

                    continue

                if "error" in result:

                    error_message = result["error"]

                    attempted.append({
                        "provider": provider_name,
                        "error": error_message
                    })

                    log_provider_request(
                        ticker=ticker,
                        provider=provider_name,
                        status="failed",
                        error_message=error_message
                    )

                    continue

                if not result.get("data_available", False):

                    error_message = "No financial data available."

                    attempted.append({
                        "provider": provider_name,
                        "error": error_message
                    })

                    log_provider_request(
                        ticker=ticker,
                        provider=provider_name,
                        status="failed",
                        error_message=error_message
                    )

                    continue

                fallback_used = (
                    provider_name != preferred_provider
                    if preferred_provider
                    else False
                )

                result["fallback_used"] = fallback_used

                result["providers_attempted"] = [
                    item["provider"]
                    for item in attempted
                ] + [provider_name]

                quality = self.assess_data_quality(
                    self.convert_to_quality_format(result)
                )

                result["quality_assessment"] = quality

                quality_details = quality.get(
                    "quality_assessment",
                    {}
                )

                try:

                    log_provider_request(
                        ticker=ticker,
                        provider=provider_name,
                        status="success",
                        fallback_used=fallback_used,
                        quality_status=quality_details.get(
                            "status"
                        ),
                        quality_score=quality_details.get(
                            "quality_score"
                        )
                    )

                except Exception as logging_error:

                    print(
                        "Provider logging warning:",
                        logging_error
                    )

                return result

            except Exception as error:

                error_message = str(error)

                attempted.append({
                    "provider": provider_name,
                    "error": error_message
                })

                try:

                    log_provider_request(
                        ticker=ticker,
                        provider=provider_name,
                        status="failed",
                        error_message=error_message
                    )

                except Exception as logging_error:

                    print(
                        "Provider logging warning:",
                        logging_error
                    )

        return {
            "error": "All registered financial providers failed.",
            "ticker": ticker,
            "providers_attempted": attempted
        }

    def get_available_providers(self):

        return list(self.providers.keys())


provider_manager = ProviderManager()
