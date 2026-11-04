
from ddgs import DDGS
from urllib.parse import urlparse


def source_priority(result):
    """
    Prioritise official investor relations and company sources.
    """

    url = result.get("href", "").lower()
    title = result.get("title", "").lower()
    domain = urlparse(url).netloc.lower()

    score = 0

    # Prioritise investor relations pages
    if "investor" in domain or "investor" in url:
        score += 5

    # Prioritise official earnings releases
    if "earnings" in url or "press-release" in url:
        score += 3

    # Prioritise official company websites
    if any(word in domain for word in [
        "microsoft.com",
        "apple.com",
        "google.com",
        "abc.xyz",
        "amazon.com",
        "aboutamazon.com",
        "nvidia.com",
        "meta.com",
        "oracle.com"
    ]):
        score += 4

    return score


def search_web(query, max_results=10):
    """
    Search the internet, prioritising recent results
    and official company sources.
    """

    try:
        with DDGS() as ddgs:

            results = list(ddgs.text(
                query,
                max_results=max_results,
                timelimit="m"
            ))

        if not results:
            return "No search results found."

        # Rank official sources higher
        results.sort(
            key=source_priority,
            reverse=True
        )

        formatted_results = []

        for result in results:

            formatted_results.append({
                "title": result.get("title", ""),
                "url": result.get("href", ""),
                "date": (
                    result.get("date")
                    or result.get("published")
                    or "Publication date unavailable"
                ),
                "description": result.get("body", "")
            })

        return formatted_results

    except Exception as error:
        return f"Search error: {error}"
