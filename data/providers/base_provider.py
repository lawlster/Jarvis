
from abc import ABC, abstractmethod
from datetime import datetime, timezone


class BaseFinancialProvider(ABC):
    """
    Common interface for financial data providers.

    Every provider must implement the methods defined here.
    """

    provider_name = "Unknown Provider"

    @abstractmethod
    def get_company_financials(self, ticker):
        """
        Retrieve financial information for a company.

        Must return a dictionary containing financial data,
        its source, and retrieval timestamp.
        """
        pass

    @abstractmethod
    def get_company_profile(self, ticker):
        """
        Retrieve basic company information.
        """
        pass

    def get_provider_metadata(self):

        return {
            "provider": self.provider_name,
            "retrieved_at_utc": datetime.now(
                timezone.utc
            ).isoformat()
        }

    def standardise_response(
        self,
        ticker,
        financials,
        profile=None
    ):

        return {
            "ticker": ticker.upper().strip(),

            "provider": self.provider_name,

            "retrieved_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),

            "company_profile": profile or {},

            "financials": financials or {},

            "data_available": bool(financials)
        }
