"""Offline contracts for the bounded historical-price backfill."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from collect_price_history import provider_code, sina_code, write_raw  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
