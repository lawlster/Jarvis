
from data.providers.provider_manager import provider_manager

from data.providers.provider_monitor import (
    get_provider_reliability_report,
    get_provider_failures,
    get_provider_quality_issues
)

from data.providers.provider_logger import (
    get_recent_provider_logs,
    get_provider_log_summary
)


def get_provider_dashboard():

    """
    Central overview of financial data provider health.
    """

    reliability = get_provider_reliability_report()

    return {

        "available_providers": (
            provider_manager.get_available_providers()
        ),

        "reliability": reliability,

        "provider_summary": (
            get_provider_log_summary()
        ),

        "recent_failures": (
            get_provider_failures(10)
        ),

        "quality_issues": (
            get_provider_quality_issues(10)
        ),

        "recent_activity": (
            get_recent_provider_logs(10)
        )

    }


def get_provider_status():

    dashboard = get_provider_dashboard()

    providers = dashboard.get(
        "reliability", {}
    ).get("providers", [])

    if not providers:

        return {
            "status": "no_data",
            "message": "No provider activity has been recorded."
        }

    low_reliability = [
        provider["provider"]
        for provider in providers
        if provider["reliability"] == "low"
    ]

    poor_quality = [
        provider["provider"]
        for provider in providers
        if provider["quality_status"] == "poor"
    ]

    if low_reliability or poor_quality:

        return {
            "status": "warning",
            "low_reliability_providers": low_reliability,
            "poor_quality_providers": poor_quality
        }

    return {
        "status": "operational",
        "providers_monitored": len(providers)
    }


def retrieve_company_data(ticker, preferred_provider=None):

    """
    Retrieve company financials and attach a
    consolidated provider status assessment.
    """

    result = (
        provider_manager.get_company_financials_with_fallback(
            ticker,
            preferred_provider=preferred_provider
        )
    )

    if "error" in result:

        return {
            "ticker": ticker.upper().strip(),
            "retrieval_status": "failed",
            "error": result["error"],
            "providers_attempted": result.get(
                "providers_attempted",
                []
            )
        }

    return {

        "ticker": result.get("ticker"),

        "retrieval_status": "success",

        "financial_data": result,

        "provider_quality": result.get(
            "quality_assessment"
        ),

        "fallback_used": result.get(
            "fallback_used",
            False
        ),

        "providers_attempted": result.get(
            "providers_attempted",
            []
        )

    }
