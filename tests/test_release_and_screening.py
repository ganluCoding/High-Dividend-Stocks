"""Offline contract tests for immutable facts releases and rule execution."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from release_protocol import resolve_release, validate_release  # noqa: E402


def create_source_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE security_master (instrument_id TEXT PRIMARY KEY, ticker TEXT, name TEXT, asset_type TEXT, exchange TEXT, listing_status TEXT, source_id TEXT, first_seen_at TEXT, last_seen_at TEXT, last_seen_run_id TEXT);
            CREATE TABLE market_daily_prices (run_id TEXT, instrument_id TEXT, trade_date TEXT, source_id TEXT, close REAL, open REAL, high REAL, low REAL, volume REAL, turnover_cny REAL, validation_status TEXT);
            CREATE TABLE stock_dividend_events (run_id TEXT, version_id TEXT, dividend_event_id TEXT, supersedes_version_id TEXT, instrument_id TEXT, profit_period_label TEXT, distribution_type TEXT, installment_no INTEGER, status TEXT, cash_per_10_shares_cny REAL, cash_dps_cny REAL, published_at_date TEXT, record_date TEXT, ex_date TEXT, payment_date TEXT, description TEXT, source_id TEXT, source_raw_sha256 TEXT, observed_at TEXT);
            CREATE TABLE etf_distribution_events (run_id TEXT, version_id TEXT, dividend_event_id TEXT, instrument_id TEXT, status TEXT, cash_per_unit_cny REAL, cumulative_distribution_cny REAL, ex_date TEXT, date_semantics TEXT, source_id TEXT, source_raw_sha256 TEXT, observed_at TEXT);
            CREATE TABLE financial_observations (run_id TEXT, instrument_id TEXT, statement_type TEXT, metric TEXT, value REAL, currency TEXT, report_date TEXT, published_at_date TEXT, updated_at_date TEXT, source_id TEXT, source_raw_sha256 TEXT, observed_at TEXT);
            """
        )
        connection.execute("INSERT INTO security_master VALUES ('CN.XSHG.600001', '600001.SH', '合成稳健股', 'stock', 'SH', 'active', 'test', '2023-01-01', '2026-08-07', 'market-run')")
        connection.execute("INSERT INTO market_daily_prices VALUES ('market-run', 'CN.XSHG.600001', '2026-08-07', 'test', 10, 10, 10, 10, 1, 1, 'approved')")
        for year in (2023, 2024, 2025, 2026):
            connection.execute(
                "INSERT INTO stock_dividend_events VALUES (?, ?, ?, NULL, 'CN.XSHG.600001', ?, '年度分红', 1, 'implemented', 5, 0.5, ?, NULL, ?, NULL, NULL, 'test', NULL, ?)",
                (f"div-{year}", f"v-{year}", f"event-{year}", str(year), f"{year}-03-01", f"{year}-04-01", f"{year}-03-01"),
            )
        connection.execute("INSERT INTO financial_observations VALUES ('fin-2026', 'CN.XSHG.600001', 'profit', 'PARENT_NETPROFIT', 100, 'CNY', '2026-03-31', '2026-04-30', NULL, 'test', NULL, '2026-04-30')")
        connection.commit()


class ReleaseAndScreeningTests(unittest.TestCase):
    def publish(self, source: Path, runtime: Path, release_id: str = "release-test") -> None:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "publish_release.py"), "--database", str(source), "--runtime-root", str(runtime), "--release-id", release_id, "--available-cutoff", "2026-08-07T15:00:00+08:00"],
            cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
        )

    def test_release_requires_hash_ready_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sqlite"
            runtime = root / "runtime"
            create_source_database(source)
            self.publish(source, runtime)
            release, manifest = resolve_release(runtime)
            self.assertEqual(manifest["release_id"], "release-test")
            self.assertTrue((release / "READY").is_file())
            with (release / "facts.sqlite").open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "hash"):
                validate_release(release)

    def test_stable_rule_uses_published_facts_and_writes_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, runtime, workbench = root / "source.sqlite", root / "runtime", root / "workbench.sqlite"
            taxonomy = root / "taxonomy.json"
            create_source_database(source)
            taxonomy.write_text(json.dumps({"instruments": [{"instrument_id": "CN.XSHG.600001", "dividend_style": "质量分红", "category": "普通工商"}]}, ensure_ascii=False), encoding="utf-8")
            self.publish(source, runtime)
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_screen.py"), "--runtime-root", str(runtime), "--workbench", str(workbench), "--taxonomy", str(taxonomy), "--rule", str(PROJECT_ROOT / "rules" / "stable_dividend_stock_v1.json")],
                cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
            )
            self.assertIn('"资料足以研究": 1', completed.stdout)
            with sqlite3.connect(workbench) as connection:
                state, reasons = connection.execute("SELECT research_state, reason_codes_json FROM screen_results_v1").fetchone()
            self.assertEqual(state, "资料足以研究")
            self.assertEqual(json.loads(reasons), ["PASS"])

    def test_strategy_run_and_desktop_core_bind_the_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, runtime, workbench = root / "source.sqlite", root / "runtime", root / "workbench.sqlite"
            create_source_database(source)
            self.publish(source, runtime, "release-strategy")
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "strategy_engine.py"), "--run", "stable_dividend_stock_research", "--runtime-root", str(runtime), "--workbench", str(workbench)],
                cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
            )
            strategy_run = json.loads(completed.stdout)
            self.assertEqual(strategy_run["release_id"], "release-strategy")
            core = subprocess.run(
                [sys.executable, str(SCRIPTS / "desktop_core.py"), "--command", "dashboard", "--runtime-root", str(runtime), "--workbench", str(workbench)],
                cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
            )
            dashboard = json.loads(core.stdout)
            self.assertTrue(dashboard["ok"])
            self.assertEqual(dashboard["result"]["release"]["release_id"], "release-strategy")


if __name__ == "__main__":
    unittest.main()
