
import yfinance as yf
import math

from datetime import datetime, timezone

from data.providers.base_provider import (
    BaseFinancialProvider
)


class YahooFinanceProvider(BaseFinancialProvider):

    provider_name = "Yahoo Finance"

    def convert_timestamp(self, timestamp):

        if not timestamp:
            return None

        try:
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            ).isoformat()

        except (ValueError, TypeError, OSError):
            return None

    def extract_row(self, statement, row_name):

        if statement.empty or row_name not in statement.index:
            return {}

        values = {}

        for date, value in statement.loc[row_name].items():

            try:
                number = float(value)

                if math.isfinite(number):
                    values[str(date.date())] = number

            except (ValueError, TypeError):
                continue

        return values

    def get_company_profile(self, ticker):

        try:

            ticker = ticker.upper().strip()

            company = yf.Ticker(ticker)
            info = company.info

            return {
                "ticker": ticker,
                "company_name": info.get("longName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "exchange": info.get("exchange"),
                "currency": info.get("currency"),
                "market_cap": info.get("marketCap"),
                "country": info.get("country")
            }

        except Exception as error:

            return {
                "error": str(error),
                "ticker": ticker.upper()
            }

    def get_company_financials(self, ticker):

        try:

            ticker = ticker.upper().strip()

            company = yf.Ticker(ticker)

            info = company.info

            income_statement = company.financials
            cash_flow = company.cashflow
            balance_sheet = company.balance_sheet

            retrieved_at = datetime.now(
                timezone.utc
            ).isoformat()

            price_data_as_of = self.convert_timestamp(
                info.get("regularMarketTime")
            )

            financials = {

                "market_cap": info.get("marketCap"),

                "current_price": (
                    info.get("currentPrice")
                    or info.get("regularMarketPrice")
                ),

                "trailing_pe": info.get("trailingPE"),

                "forward_pe": info.get("forwardPE"),

                "revenue": self.extract_row(
                    income_statement,
                    "Total Revenue"
                ),

                "operating_income": self.extract_row(
                    income_statement,
                    "Operating Income"
                ),

                "net_income": self.extract_row(
                    income_statement,
                    "Net Income"
                ),

                "free_cash_flow": self.extract_row(
                    cash_flow,
                    "Free Cash Flow"
                ),

                "total_debt": self.extract_row(
                    balance_sheet,
                    "Total Debt"
                ),

                "cash": self.extract_row(
                    balance_sheet,
                    "Cash And Cash Equivalents"
                ),

                "shares_outstanding": info.get(
                    "sharesOutstanding"
                )
            }

            return {
                "ticker": ticker,

                "provider": self.provider_name,

                "retrieved_at_utc": retrieved_at,

                "price_data_as_of_utc": price_data_as_of,

                "financial_statements_latest_period": {

                    "income_statement": (
                        str(income_statement.columns[0].date())
                        if not income_statement.empty
                        else None
                    ),

                    "cash_flow": (
                        str(cash_flow.columns[0].date())
                        if not cash_flow.empty
                        else None
                    ),

                    "balance_sheet": (
                        str(balance_sheet.columns[0].date())
                        if not balance_sheet.empty
                        else None
                    )
                },

                "financials": financials,

                "data_available": (
                    not income_statement.empty
                    or not cash_flow.empty
                    or not balance_sheet.empty
                )
            }

        except Exception as error:

            return {
                "error": str(error),
                "ticker": ticker.upper(),
                "provider": self.provider_name
            }
