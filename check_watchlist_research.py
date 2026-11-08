
from agents.watchlist_research import research_watchlist

results = research_watchlist()

print("\nALL RESEARCH COMPLETED")

for result in results:
    print(f"\nCompany: {result['ticker']}")
    print(result["report"][:500])
    print("\n" + "-" * 60)
