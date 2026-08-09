#!/usr/bin/env python3
"""Build and quality-check the first composite six-instrument data snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = PROJECT_ROOT / "config" / "spike_universe.json"
SOURCE_RUNS = {
    "base": "run_20260809_initial",
    "etf_price_fallback": "run_20260809_prices_sina",
    "stock_price_fallback": "run_20260809_prices_baostock",
}
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_canonical(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_manifest(run_id: str) -> tuple[Path, dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "manifests" / f"{run_id}.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def verify_source_run(run_id: str, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for group in ("raw_files", "normalized_files"):
        for item in manifest[group]:
            path = PROJECT_ROOT / item["path"]
            actual_hash = sha256_file(path)
            checks.append(
                {
                    "check": "source_file_hash",
                    "run_id": run_id,
                    "path": item["path"],
                    "passed": actual_hash == item["sha256"],
                    "expected_sha256": item["sha256"],
                    "actual_sha256": actual_hash,
                }
            )
            if group == "raw_files":
                metadata = json.loads((PROJECT_ROOT / item["meta_path"]).read_text(encoding="utf-8"))
                checks.append(
                    {
                        "check": "raw_envelope_hash_link",
                        "run_id": run_id,
                        "path": item["meta_path"],
                        "passed": metadata["content_sha256"] == actual_hash,
                        "expected_sha256": actual_hash,
                        "actual_sha256": metadata["content_sha256"],
                    }
                )
    return checks


def read_normalized(run_id: str, filename: str) -> pd.DataFrame:
    path = PROJECT_ROOT / "data" / "normalized" / run_id / filename
    return pd.read_csv(path, dtype=str)


def check(name: str, passed: bool, details: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "details": details}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-run-id", required=True)
    args = parser.parse_args()
    output_run_id = args.output_run_id
    output_dir = PROJECT_ROOT / "data" / "curated" / output_run_id
    manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{output_run_id}.json"
    quality_path = PROJECT_ROOT / "data" / "quality" / f"{output_run_id}.json"
    if output_dir.exists() or manifest_path.exists() or quality_path.exists():
        raise SystemExit(f"Composite run already exists and will not be overwritten: {output_run_id}")

    universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    instrument_ids = [item["instrument_id"] for item in universe["instruments"]]
    stock_ids = [
        item["instrument_id"]
        for item in universe["instruments"]
        if item["asset_type"] == "stock"
    ]
    etf_ids = [
        item["instrument_id"]
        for item in universe["instruments"]
        if item["asset_type"] == "etf"
    ]
    as_of = pd.Timestamp(universe["research_as_of"]).tz_localize(None).normalize()

    source_manifests: dict[str, dict[str, Any]] = {}
    source_manifest_paths: dict[str, Path] = {}
    source_checks: list[dict[str, Any]] = []
    for role, run_id in SOURCE_RUNS.items():
        path, manifest = load_manifest(run_id)
        source_manifest_paths[role] = path
        source_manifests[role] = manifest
        source_checks.extend(verify_source_run(run_id, manifest))

    prices = pd.concat(
        [
            read_normalized(SOURCE_RUNS["base"], "daily_prices.csv"),
            read_normalized(SOURCE_RUNS["etf_price_fallback"], "daily_prices.csv"),
            read_normalized(SOURCE_RUNS["stock_price_fallback"], "daily_prices.csv"),
        ],
        ignore_index=True,
    )
    prices["trade_date_parsed"] = pd.to_datetime(prices["trade_date"], errors="coerce")
    prices["close_numeric"] = pd.to_numeric(prices["close"], errors="coerce")
    invalid_price_rows = int(
        (
            prices["trade_date_parsed"].isna()
            | prices["close_numeric"].isna()
            | (prices["close_numeric"] <= 0)
            | (prices["trade_date_parsed"] > as_of)
        ).sum()
    )
    prices = prices[
        prices["trade_date_parsed"].notna()
        & prices["close_numeric"].notna()
        & (prices["close_numeric"] > 0)
        & (prices["trade_date_parsed"] <= as_of)
    ].copy()
    duplicate_price_rows = int(prices.duplicated(["instrument_id", "trade_date"], keep=False).sum())
    prices = prices.sort_values(["instrument_id", "trade_date_parsed"])
    prices = prices.drop(columns=["trade_date_parsed", "close_numeric"])

    stock_dividends = read_normalized(SOURCE_RUNS["base"], "stock_dividend_events.csv")
    etf_distributions = read_normalized(SOURCE_RUNS["base"], "etf_distribution_events.csv")
    financials = read_normalized(SOURCE_RUNS["base"], "financial_observations.csv")

    quality_checks = list(source_checks)
    quality_checks.extend(
        [
            check("principal_is_frozen", universe["principal_cny"] == "200000.00", universe["principal_cny"]),
            check("price_instrument_coverage", sorted(prices["instrument_id"].unique()) == sorted(instrument_ids), prices["instrument_id"].value_counts().to_dict()),
            check("price_duplicate_rows", duplicate_price_rows == 0, duplicate_price_rows),
            check("price_invalid_rows", invalid_price_rows == 0, invalid_price_rows),
            check("price_as_of", prices.groupby("instrument_id")["trade_date"].max().eq(as_of.date().isoformat()).all(), prices.groupby("instrument_id")["trade_date"].max().to_dict()),
            check("stock_dividend_coverage", sorted(stock_dividends["instrument_id"].unique()) == sorted(stock_ids), stock_dividends["instrument_id"].value_counts().to_dict()),
            check("stock_dividend_version_duplicates", not stock_dividends["version_id"].duplicated().any(), int(stock_dividends["version_id"].duplicated().sum())),
            check("etf_distribution_event_bounds", (pd.to_datetime(etf_distributions["ex_date"], errors="coerce") <= as_of).all(), int(len(etf_distributions))),
            check("financial_stock_coverage", sorted(financials["instrument_id"].unique()) == sorted(stock_ids), financials["instrument_id"].value_counts().to_dict()),
            check("financial_statement_coverage", financials.groupby("instrument_id")["statement_type"].nunique().eq(3).all(), financials.groupby("instrument_id")["statement_type"].nunique().to_dict()),
        ]
    )

    for column in ("published_at_date", "updated_at_date", "report_date"):
        values = pd.to_datetime(financials[column], errors="coerce")
        quality_checks.append(
            check(
                f"financial_{column}_pit",
                (values.dropna() <= as_of).all(),
                None if values.dropna().empty else values.dropna().max().date().isoformat(),
            )
        )

    raw_etf_distribution_ids = {
        item["instrument_id"]
        for item in source_manifests["base"]["raw_files"]
        if item["dataset"] == "etf_distributions_cumulative"
    }
    quality_checks.append(
        check(
            "etf_distribution_source_coverage",
            raw_etf_distribution_ids == set(etf_ids),
            sorted(raw_etf_distribution_ids),
        )
    )
    golden_validation = json.loads(
        (PROJECT_ROOT / "data" / "quality" / "golden_validation.json").read_text(encoding="utf-8")
    )
    quality_checks.append(
        check("golden_fixture_validation", golden_validation["status"] == "pass", golden_validation["status"])
    )

    output_dir.mkdir(parents=True)
    outputs: dict[str, pd.DataFrame] = {
        "daily_prices.csv": prices,
        "stock_dividend_events.csv": stock_dividends,
        "etf_distribution_events.csv": etf_distributions,
        "financial_observations.csv": financials,
    }

    summary_rows: list[dict[str, Any]] = []
    one_year_cutoff = as_of - pd.DateOffset(years=1)
    for instrument in universe["instruments"]:
        instrument_id = instrument["instrument_id"]
        instrument_prices = prices[prices["instrument_id"] == instrument_id].copy()
        instrument_prices["trade_date_parsed"] = pd.to_datetime(instrument_prices["trade_date"])
        instrument_prices["close_numeric"] = pd.to_numeric(instrument_prices["close"])
        latest = instrument_prices.sort_values("trade_date_parsed").iloc[-1]
        if instrument["asset_type"] == "stock":
            distributions = stock_dividends[stock_dividends["instrument_id"] == instrument_id].copy()
            distributions["event_date"] = pd.to_datetime(distributions["ex_date"], errors="coerce")
            distributions["cash"] = pd.to_numeric(distributions["cash_dps_cny"], errors="coerce")
            event_count = int(len(distributions))
        else:
            distributions = etf_distributions[etf_distributions["instrument_id"] == instrument_id].copy()
            if distributions.empty:
                distributions = pd.DataFrame(columns=["event_date", "cash"])
            else:
                distributions["event_date"] = pd.to_datetime(distributions["ex_date"], errors="coerce")
                distributions["cash"] = pd.to_numeric(distributions["cash_per_unit_cny"], errors="coerce")
            event_count = int(len(distributions))
        ttm_cash = distributions.loc[
            (distributions["event_date"] > one_year_cutoff)
            & (distributions["event_date"] <= as_of),
            "cash",
        ].sum()
        summary_rows.append(
            {
                "instrument_id": instrument_id,
                "ticker": instrument["ticker"],
                "name": instrument["name"],
                "asset_type": instrument["asset_type"],
                "price_rows": int(len(instrument_prices)),
                "price_start": instrument_prices["trade_date_parsed"].min().date().isoformat(),
                "price_end": instrument_prices["trade_date_parsed"].max().date().isoformat(),
                "latest_unadjusted_close_cny": format(float(latest["close_numeric"]), ".10g"),
                "price_source_ids": "|".join(sorted(instrument_prices["source_id"].unique())),
                "distribution_event_rows": event_count,
                "ttm_implemented_cash_per_share_or_unit_cny": format(float(ttm_cash), ".10g"),
                "primary_verification_status": "partial",
                "research_status": "未评估",
            }
        )
    summary = pd.DataFrame(summary_rows)
    outputs["collection_summary.csv"] = summary

    output_files = []
    for filename, frame in outputs.items():
        path = output_dir / filename
        frame.to_csv(path, index=False, encoding="utf-8")
        output_files.append(
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "rows": int(len(frame)),
                "columns": int(len(frame.columns)),
                "sha256": sha256_file(path),
            }
        )

    all_passed = all(item["passed"] for item in quality_checks)
    quality_report = {
        "schema_version": "data-quality-report-v1",
        "run_id": output_run_id,
        "research_as_of": universe["research_as_of"],
        "status": "pass" if all_passed else "fail",
        "checks": quality_checks,
        "known_collection_failures": {
            role: manifest["failures"]
            for role, manifest in source_manifests.items()
            if manifest["failures"]
        },
        "scope_note": "Pass applies only to collected baseline domains; it is not a completed investment-research or suitability verdict.",
    }
    quality_path.parent.mkdir(parents=True, exist_ok=True)
    quality_path.write_text(
        json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    source_manifest_entries = []
    for role, path in source_manifest_paths.items():
        source_manifest_entries.append(
            {
                "role": role,
                "run_id": SOURCE_RUNS[role],
                "path": str(path.relative_to(PROJECT_ROOT)),
                "sha256": sha256_file(path),
            }
        )
    config_entries = []
    for relative in (
        "config/spike_universe.json",
        "config/source_matrix.csv",
        "config/primary_source_registry.json",
        "config/formula_registry.json",
        "config/reason_codes.json",
        "tests/fixtures/golden_fixtures.json",
        "scripts/collect_spike_data.py",
        "scripts/validate_golden_fixtures.py",
        "scripts/build_spike_snapshot.py",
        "data/quality/golden_validation.json",
    ):
        path = PROJECT_ROOT / relative
        config_entries.append({"path": relative, "sha256": sha256_file(path)})

    deterministic_inputs = {
        "principal_cny": universe["principal_cny"],
        "research_as_of": universe["research_as_of"],
        "source_manifest_hashes": [
            {"role": item["role"], "sha256": item["sha256"]}
            for item in source_manifest_entries
        ],
        "config_files": config_entries,
        "output_files": [
            {
                "filename": Path(item["path"]).name,
                "rows": item["rows"],
                "columns": item["columns"],
                "sha256": item["sha256"],
            }
            for item in output_files
        ],
    }
    manifest = {
        "schema_version": "composite-research-run-manifest-v1",
        "run_id": output_run_id,
        "principal_cny": universe["principal_cny"],
        "research_as_of": universe["research_as_of"],
        "status": "baseline_data_ready" if all_passed else "quality_failed",
        "source_manifests": source_manifest_entries,
        "config_files": config_entries,
        "output_files": output_files,
        "quality_report": {
            "path": str(quality_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(quality_path),
        },
        "deterministic_content_hash": sha256_canonical(deterministic_inputs),
        "usage_scope": "internal_research_only",
        "research_status": "未评估",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_id": output_run_id,
                "status": manifest["status"],
                "price_rows": int(len(prices)),
                "stock_dividend_events": int(len(stock_dividends)),
                "etf_distribution_events": int(len(etf_distributions)),
                "financial_observations": int(len(financials)),
                "deterministic_content_hash": manifest["deterministic_content_hash"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
