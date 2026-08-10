"""Offline contracts for the bounded historical-price backfill."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from collect_price_history import collect_stock_history_with_relogin, provider_code, select_instruments, sina_code, write_raw  # noqa: E402


class PriceHistoryCollectionTests(unittest.TestCase):
    def test_provider_code_formats_are_source_specific(self) -> None:
        self.assertEqual(provider_code("600900.SH"), "sh.600900")
        self.assertEqual(sina_code("510880.SH"), "sh510880")
        self.assertEqual(sina_code("159001.SZ"), "sz159001")

    def test_raw_envelope_records_source_hash_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "prices.csv"
            instrument = {"instrument_id": "CN.XSHG.600900", "ticker": "600900.SH"}
            digest = write_raw(
                path,
                instrument,
                [{"trade_date": "2026-08-07", "close": 27.75, "adjustment": "unadjusted"}],
                "2026-08-10T00:00:00+00:00",
                "test_history_source",
            )
            metadata = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_id"], "test_history_source")
            self.assertEqual(metadata["content_sha256"], digest)
            self.assertEqual(metadata["row_count"], 1)
            self.assertEqual(metadata["start_date"], "2026-08-07")

    def test_full_market_selection_does_not_require_dividend_history(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE instruments (instrument_id TEXT, ticker TEXT, name TEXT, asset_type TEXT, active INTEGER);
            CREATE TABLE price_daily (instrument_id TEXT, trade_date TEXT, adjustment TEXT);
            INSERT INTO instruments VALUES ('CN.XSHG.600000', '600000.SH', '浦发银行', 'stock', 1);
            """
        )
        selected = select_instruments(connection, "stock", 1000, "2019-01-01", 0, 10)
        self.assertEqual([item["ticker"] for item in selected], ["600000.SH"])

    def test_stock_history_relogs_in_after_expired_baostock_session(self) -> None:
        class Result:
            error_code = "0"
            error_msg = ""

        class FakeBaoStock:
            def __init__(self) -> None:
                self.logins = 0
                self.logouts = 0

            def login(self):
                self.logins += 1
                return Result()

            def logout(self) -> None:
                self.logouts += 1

        provider = FakeBaoStock()
        with patch("collect_price_history.collect_baostock_daily", side_effect=[RuntimeError("BaoStock query failed: 用户未登录"), [{"trade_date": "2026-08-10"}]]) as mocked:
            rows = collect_stock_history_with_relogin({"ticker": "600900.SH"}, "2019-01-01", "2026-08-10", provider)
        self.assertEqual(rows, [{"trade_date": "2026-08-10"}])
        self.assertEqual(provider.logins, 1)
        self.assertEqual(provider.logouts, 1)
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
