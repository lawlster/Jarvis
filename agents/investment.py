
from tools.finance import get_company_financials
from tools.research import search_web

from agents.report_generator import generate_investment_report


def analyse_company(ticker):

    ticker = ticker.upper().strip()

    print(f"\nStarting investment research for {ticker}...")

    # Step 1: Retrieve financial information
    print("Retrieving financial statements...")

    financials = get_company_financials(ticker)

    if "error" in financials:
        return f"Financial data error: {financials['error']}"

    # Step 2: Research recent company developments
    print("Researching recent company developments...")

    news = search_web(
        f"{ticker} latest earnings results company news developments"
    )

    # Step 3: Combine research
    research_data = {
        "ticker": ticker,
        "financial_information": financials,
        "recent_research": news
    }

    # Step 4: Generate report using local AI
    print("Generating investment research report...")

    report = generate_investment_report(research_data)

    return report
