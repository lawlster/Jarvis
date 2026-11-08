import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from agents import us_security_classifier as classifier
from agents import us_security_evidence as evidence_collector


NASDAQ_FIXTURE = """Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares
Y|AAPL|Apple Inc. - Common Stock|Q|Q|N|100|N|N|AAPL|AAPL|N
Y|QQQ|Invesco QQQ Trust, Series 1|Q|Q|Y|100|N|N|QQQ|QQQ|N
Y|BABA|Alibaba Group Holding Limited American Depositary Shares|N|N|N|100|N|N|BABA|BABA|N
Y|BAC-PR|Bank of America Corporation Depositary Shares 5.875% Series AA Preferred Stock|N|N|N|100|N|N|BAC-PR|BAC-PR|N
Y|WXYZW|WXYZ Corporation Warrants|Q|Q|N|100|N|N|WXYZW|WXYZW|N
File Creation Time: 1108202609:00|||||||||||
"""


def listing(listing_id, ticker, exchange, cik):
    return {
        "id": listing_id,
        "ticker": ticker,
        "company_name": f"Example {ticker}",
        "exchange": exchange,
        "country": "USA",
        "currency": "USD",
        "security_type": "equity",
        "is_active": 1,
        "data_source": "SEC EDGAR company_tickers_exchange.json",
        "sec_cik": cik,
    }


class FakeResponse:
    def __init__(self, content, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.headers = {}
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_responses.pop(0)


class USSecurityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_nasdaq_directory_parser_uses_structured_exchange_and_etf_fields(self):
        rows = evidence_collector.parse_nasdaq_directory(NASDAQ_FIXTURE)
        self.assertEqual(len(rows), 5)
        self.assertEqual((rows[0]["ticker"], rows[0]["exchange"]), ("AAPL", "NASDAQ"))
        self.assertTrue(rows[1]["etf"])
        self.assertEqual((rows[2]["ticker"], rows[2]["exchange"]), ("BABA", "NYSE"))
        self.assertEqual(evidence_collector.parse_issue_form_description(rows[0]["security_name"])[0], "common_share")
        self.assertEqual(evidence_collector.parse_issue_form_description(rows[2]["security_name"])[0], "adr_ads")
        self.assertEqual(evidence_collector.parse_issue_form_description(rows[3]["security_name"])[0], "preferred_share")
        self.assertEqual(evidence_collector.parse_issue_form_description(rows[4]["security_name"])[0], "warrant")
        self.assertEqual(rows[3]["share_class"], "Series AA")

    def test_official_evidence_matches_ticker_and_exchange_and_yields_four_statuses(self):
        listings = [
            listing(1, "AAPL", "NASDAQ", "0000000001"),
            listing(2, "QQQ", "NASDAQ", "0000000002"),
            listing(3, "BABA", "NYSE", "0000000003"),
            listing(4, "BAC-PR", "NYSE", "0000000004"),
            listing(5, "O", "NYSE", "0000000005"),
            listing(6, "WXYZW", "NASDAQ", "0000000006"),
            listing(7, "SAME", "NYSE", "0000000007"),
            listing(8, "ONLY", "NYSE", "0000000008"),
            listing(9, "SAME", "NASDAQ", "0000000009"),
        ]
        nasdaq = evidence_collector.parse_nasdaq_directory(NASDAQ_FIXTURE)
        metadata = {"fetched_at_utc": "2026-11-08T12:00:00+00:00"}
        for row in nasdaq:
            row["source_metadata"] = metadata
        nyse_sources = {
            "REIT": {
                "metadata": metadata,
                "rows": [{"total": 1, "url": "https://www.nyse.com/quote/XNYS:O", "normalizedTicker": "O", "symbolExchangeTicker": "O", "instrumentName": "REALTY INCOME CORP"}],
            },
            "PREFERRED_STOCK": {
                "metadata": metadata,
                "rows": [{"total": 1, "url": "https://www.nyse.com/quote/XNYS:BAC-PR", "normalizedTicker": "BAC-PR", "symbolExchangeTicker": "BAC-PR", "instrumentName": "BANK OF AMERICA PREFERRED STOCK"}],
            },
            "WARRANT": {
                "metadata": metadata,
                "rows": [{"total": 1, "url": "https://www.nyse.com/quote/XNYS:SAME", "normalizedTicker": "SAME", "symbolExchangeTicker": "SAME", "instrumentName": "SAME INC WARRANT"}],
            },
            "EQUITY": {
                "metadata": metadata,
                "rows": [
                    {"total": 2, "url": "https://www.nyse.com/quote/XNYS:SAME", "normalizedTicker": "SAME", "symbolExchangeTicker": "SAME", "instrumentName": "SAME INC Common Stock"},
                    {"total": 2, "url": "https://www.nyse.com/quote/XNYS:ONLY", "normalizedTicker": "ONLY", "symbolExchangeTicker": "ONLY", "instrumentName": "ONLY INC"},
                ],
            },
        }
        nyse = evidence_collector.parse_nyse_records(nyse_sources)
        evidence_rows, unmatched = evidence_collector.build_evidence_rows(listings, nasdaq, nyse)
        classified = classifier.classify_listings(listings, evidence_rows, "2026-11-08")
        by_key = {(row["ticker"], row["exchange"]): row for row in classified}
        self.assertEqual(by_key[("QQQ", "NASDAQ")]["classification_status"], "confirmed")
        self.assertEqual(by_key[("QQQ", "NASDAQ")]["security_form"], "exchange_traded_product")
        self.assertEqual(by_key[("AAPL", "NASDAQ")]["classification_status"], "provisional")
        self.assertEqual(by_key[("AAPL", "NASDAQ")]["security_form"], "common_share")
        self.assertEqual(by_key[("BABA", "NYSE")]["security_form"], "adr_ads")
        self.assertEqual(by_key[("BAC-PR", "NYSE")]["security_form"], "preferred_share")
        self.assertEqual(by_key[("O", "NYSE")]["issuer_structure"], "reit")
        self.assertEqual(by_key[("WXYZW", "NASDAQ")]["security_form"], "warrant")
        self.assertEqual(by_key[("SAME", "NYSE")]["classification_status"], "conflicting")
        self.assertEqual(by_key[("SAME", "NASDAQ")]["classification_status"], "unknown")
        self.assertEqual(by_key[("ONLY", "NYSE")]["classification_status"], "unknown")
        self.assertEqual(unmatched, {"nasdaq": 0, "nyse": 0})
        self.assertTrue(all(row["manual_review_status"] == "required" for key, row in by_key.items() if key != ("QQQ", "NASDAQ")))

    def test_cache_records_timestamp_source_and_hash_and_reuses_fresh_snapshot(self):
        content = NASDAQ_FIXTURE.encode("utf-8")
        fixed_now = lambda: datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        session = FakeSession(get_responses=[FakeResponse(content)])
        collector = evidence_collector.OfficialExchangeCollector(
            self.root / "cache", session=session, min_interval=0,
            sleeper=lambda delay: None, now=fixed_now,
        )
        records, metadata = collector.fetch_nasdaq(refresh=True)
        self.assertEqual(len(records), 5)
        self.assertEqual(metadata["source_url"], evidence_collector.NASDAQ_DIRECTORY_URL)
        self.assertEqual(metadata["sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(metadata["fetched_at_utc"].startswith("2026-11-08"))
        self.assertTrue((self.root / "cache" / metadata["snapshot_file"]).exists())

        no_network = Mock()
        no_network.get.side_effect = AssertionError("fresh cache should avoid network")
        cached = evidence_collector.OfficialExchangeCollector(
            self.root / "cache", session=no_network, min_interval=0,
            sleeper=lambda delay: None, now=fixed_now,
        )
        cached_rows, cached_metadata = cached.fetch_nasdaq()
        self.assertEqual(len(cached_rows), len(records))
        self.assertEqual(cached_metadata["sha256"], metadata["sha256"])
        no_network.get.assert_not_called()

    def test_retry_after_is_honored_for_rate_limit_response(self):
        content = NASDAQ_FIXTURE.encode("utf-8")
        session = FakeSession(get_responses=[
            FakeResponse(b"too many", 429, {"Retry-After": "2"}),
            FakeResponse(content),
        ])
        sleeps = []
        collector = evidence_collector.OfficialExchangeCollector(
            self.root / "cache", session=session, min_interval=0,
            sleeper=sleeps.append, retries=1,
        )
        self.assertEqual(collector._request("GET", evidence_collector.NASDAQ_DIRECTORY_URL), content)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(len(session.calls), 2)

    def test_nyse_collector_requests_all_listed_categories_with_one_page_cap(self):
        session = FakeSession(post_responses=[FakeResponse(b"[]") for _ in evidence_collector.NYSE_CATEGORIES])
        collector = evidence_collector.OfficialExchangeCollector(
            self.root / "cache", session=session, min_interval=0, sleeper=lambda delay: None
        )
        results = collector.fetch_nyse(refresh=True)
        self.assertEqual(set(results), set(evidence_collector.NYSE_CATEGORIES))
        self.assertEqual(len(session.calls), len(evidence_collector.NYSE_CATEGORIES))
        for method, url, kwargs in session.calls:
            self.assertEqual(method, "POST")
            self.assertEqual(url, evidence_collector.NYSE_FILTER_URL)
            self.assertEqual(kwargs["json"]["maxResultsPerPage"], 10000)
            self.assertEqual(kwargs["json"]["instrumentType"] in evidence_collector.NYSE_CATEGORIES, True)


if __name__ == "__main__":
    unittest.main()
