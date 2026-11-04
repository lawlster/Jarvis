
from agents.watchlist import get_watchlist
from agents.investment import analyse_company


def research_watchlist():

    companies = get_watchlist()

    if not companies:
        return "Your watchlist is empty."

    results = []

    print("\n" + "=" * 60)
    print("AiAssistant - WATCHLIST RESEARCH")
    print("=" * 60)

    print(f"\nFound {len(companies)} companies to research.\n")

    for index, company in enumerate(companies, start=1):

        ticker = company["ticker"]

        print(f"\n[{index}/{len(companies)}] Researching {ticker}...")
        print("-" * 50)

        result = analyse_company(ticker)

        results.append({
            "ticker": ticker,
            "report": result
        })

    print("\n" + "=" * 60)
    print("WATCHLIST RESEARCH COMPLETED")
    print("=" * 60)

    return results
