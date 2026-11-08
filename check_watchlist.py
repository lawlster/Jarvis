
from agents.watchlist import (
    initialise_watchlist,
    add_company,
    remove_company,
    get_watchlist
)

# Initialise database table
initialise_watchlist()

# Add companies
print(add_company("MSFT", "Microsoft"))
print(add_company("GOOGL", "Alphabet"))
print(add_company("NVDA", "NVIDIA"))

# Display watchlist
print("\nYOUR WATCHLIST:")
for company in get_watchlist():
    print(company)

# Test duplicate protection
print("\nDUPLICATE TEST:")
print(add_company("MSFT", "Microsoft"))
