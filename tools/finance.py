"""Company financials through the provider manager.

Step 54: transient provider failures (crumb/timeout/connection
errors from Yahoo) are retried with backoff, and a persistent
provider failure is reported distinctly from genuine no-data —
``provider_unavailable: True`` — so callers can tell "the
provider could not be reached" apart from "this ticker has no
financial data".  Nothing is ever invented on failure.
"""

import time

from data.providers.provider_manager import provider_manager

# Substrings (case-insensitive) that mark a provider error as
# transient — worth one more attempt.  Anything else (an
# unregistered provider, an invalid response, a programming
# error) is returned immediately, unretried.
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "crumb",
    "429",
    "too many requests",
    "502",
    "503",
    "504",
    "temporarily",
    "reset by peer",
    "eof",
    "network",
    "unreachable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)

DEFAULT_MAX_ATTEMPTS = 3

DEFAULT_RETRY_DELAY_SECONDS = 1.0


def _is_transient(error_text, providers_attempted=None):
    """True when an error looks like a transient provider fault.

    Checks the top-level message and every per-provider attempt
    error (the provider manager aggregates failures, so the
    underlying crumb/timeout text lives in ``providers_attempted``).
    """
    texts = []
    if error_text:
        texts.append(str(error_text))
    for attempt in providers_attempted or []:
        if isinstance(attempt, dict) and attempt.get("error"):
            texts.append(str(attempt["error"]))
    joined = " ".join(texts).lower()
    return any(marker in joined for marker in _TRANSIENT_MARKERS)


def _fetch_once(ticker, provider_name="yahoo"):
    """One unretried fetch — the pre-Step-54 behaviour.

    Returns the provider-shaped dict.  A provider failure comes
    back as ``{"error": ..., "ticker": ..., "providers_attempted":
    [...]}``; genuine no-data comes back with
    ``data_available: False`` and no ``"error"`` key — that
    distinction is what the retry loop below relies on.
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
                "ticker": ticker,
                "providers_attempted": result.get(
                    "providers_attempted", []),
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

            # Step 58: the reporting currency of the financial
            # statements (Yahoo's financialCurrency), passed
            # through so the live fallback can refuse a DCF/price
            # comparison across currencies (the KSPI case).
            "financial_currency": profile.get("financial_currency"),

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


def get_company_financials(ticker, provider_name="yahoo",
                           max_attempts=DEFAULT_MAX_ATTEMPTS,
                           retry_delay_seconds=(
                               DEFAULT_RETRY_DELAY_SECONDS),
                           sleep=None):
    """
    Retrieve company financial information through
    the central provider manager.

    Maintains the original response structure so existing
    investment modules remain compatible.

    Transient provider failures (timeout, connection, crumb
    fetch, 5xx, rate limit) are retried up to ``max_attempts``
    times with exponential backoff (``retry_delay_seconds``,
    doubled per attempt; ``sleep`` is injectable for tests and
    defaults to :func:`time.sleep`).  A genuine no-data
    response (``data_available: False`` with no ``"error"`` —
    e.g. no such ticker) is returned immediately, never
    retried.  A persistent transient failure returns
    ``data_available: False`` with ``provider_unavailable:
    True`` and the last error, so callers can tell it apart
    from no-data.  Nothing is invented on failure.
    """

    if sleep is None:
        sleep = time.sleep

    ticker = ticker.upper().strip()
    attempts = max(1, int(max_attempts))
    last_error = None
    last_attempted = []

    for attempt in range(1, attempts + 1):
        result = _fetch_once(ticker, provider_name)

        if not isinstance(result, dict) or "error" not in result:
            # Success, or genuine no-data — either way there is
            # nothing to retry.
            return result

        last_error = result.get("error")
        last_attempted = result.get("providers_attempted", [])

        if not _is_transient(last_error, last_attempted):
            return result

        if attempt < attempts:
            sleep(retry_delay_seconds * (2 ** (attempt - 1)))

    # Report the most informative error: the underlying
    # per-provider message (e.g. the crumb/timeout text) when
    # the manager recorded one, else the aggregate message.
    detail = last_error
    for attempt_record in reversed(last_attempted or []):
        if isinstance(attempt_record, dict) and attempt_record.get(
                "error"):
            detail = attempt_record["error"]
            break
    return {
        "error": detail,
        "ticker": ticker,
        "data_available": False,
        "provider_unavailable": True,
        "provider_error": detail,
        "providers_attempted": last_attempted,
        "attempts": attempts,
    }
