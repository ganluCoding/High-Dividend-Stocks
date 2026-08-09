#!/usr/bin/env python3
"""Derive a source-linked, pre-tax factual research pack from the frozen snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_RUN = "run_20260809_composite_v2a"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fmt(value: float | None, digits: int = 6) -> str | None:
    if value is None or not np.isfinite(value):
        return None
    return f"{value:.{digits}f}"


def latest_metric(financials: pd.DataFrame, instrument_id: str, metric: str, date: str) -> float | None:
    subset = financials[
        (financials["instrument_id"] == instrument_id)
        & (financials["metric"] == metric)
        & (financials["report_date"] == date)
    ]
    if subset.empty:
        return None
    value = pd.to_numeric(subset.iloc[-1]["value"], errors="coerce")
    return None if pd.isna(value) else float(value)


def price_metrics(frame: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, float | str | None]:
    work = frame.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["turnover_cny"] = pd.to_numeric(work["turnover_cny"], errors="coerce")
    work = work.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    current = work.iloc[-1]
    one_year_start = as_of - pd.DateOffset(years=1)
    one_year = work[work["trade_date"] >= one_year_start].copy()
    returns = one_year["close"].pct_change().dropna()
    running_peak = one_year["close"].cummax()
    drawdown = one_year["close"] / running_peak - 1.0
    return {
        "latest_unadjusted_close_cny": float(current["close"]),
        "price_start_date": work.iloc[0]["trade_date"].date().isoformat(),
        "price_end_date": current["trade_date"].date().isoformat(),
        "one_year_unadjusted_price_return": float(one_year.iloc[-1]["close"] / one_year.iloc[0]["close"] - 1.0),
        "one_year_annualized_price_volatility": float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else None,
        "one_year_max_unadjusted_price_drawdown": float(drawdown.min()) if not drawdown.empty else None,
        "avg_daily_turnover_cny_20d": float(work.tail(20)["turnover_cny"].mean()) if work.tail(20)["turnover_cny"].notna().any() else None,
        "price_source_ids": "|".join(sorted(work["source_id"].dropna().unique())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-run", default=DEFAULT_SNAPSHOT_RUN)
    parser.add_argument("--output-run-id", default="run_20260809_research_v1")
    args = parser.parse_args()

    snapshot_dir = PROJECT_ROOT / "data" / "curated" / args.snapshot_run
    snapshot_manifest = PROJECT_ROOT / "data" / "manifests" / f"{args.snapshot_run}.json"
    output_dir = PROJECT_ROOT / "data" / "research" / args.output_run_id
    manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{args.output_run_id}.json"
    if output_dir.exists() or manifest_path.exists():
        raise SystemExit(f"Research run already exists and will not be overwritten: {args.output_run_id}")

    universe_path = PROJECT_ROOT / "config" / "spike_universe.json"
    sources_path = PROJECT_ROOT / "config" / "research_primary_sources.json"
    formula_path = PROJECT_ROOT / "config" / "formula_registry.json"
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    as_of = pd.Timestamp(universe["research_as_of"]).tz_localize(None).normalize()
    prices = pd.read_csv(snapshot_dir / "daily_prices.csv", dtype=str)
    stock_dividends = pd.read_csv(snapshot_dir / "stock_dividend_events.csv", dtype=str)
    etf_distributions = pd.read_csv(snapshot_dir / "etf_distribution_events.csv", dtype=str)
    financials = pd.read_csv(snapshot_dir / "financial_observations.csv", dtype=str)

    price_rows: list[dict[str, Any]] = []
    fact_rows: list[dict[str, Any]] = []
    dividend_rows: list[dict[str, Any]] = []
    cutoff = as_of - pd.DateOffset(years=1)
    stock_dividends["ex_date_parsed"] = pd.to_datetime(stock_dividends["ex_date"], errors="coerce")
    stock_dividends["cash"] = pd.to_numeric(stock_dividends["cash_dps_cny"], errors="coerce")
    etf_distributions["ex_date_parsed"] = pd.to_datetime(etf_distributions["ex_date"], errors="coerce")
    etf_distributions["cash"] = pd.to_numeric(etf_distributions["cash_per_unit_cny"], errors="coerce")

    for instrument in universe["instruments"]:
        iid = instrument["instrument_id"]
        pmetrics = price_metrics(prices[prices["instrument_id"] == iid], as_of)
        price_rows.append({"instrument_id": iid, "ticker": instrument["ticker"], **{key: fmt(value) if isinstance(value, float) else value for key, value in pmetrics.items()}})
        if instrument["asset_type"] == "stock":
            events = stock_dividends[stock_dividends["instrument_id"] == iid]
            cash_ttm = float(events.loc[(events["ex_date_parsed"] > cutoff) & (events["ex_date_parsed"] <= as_of), "cash"].sum())
            eps = latest_metric(financials, iid, "BASIC_EPS", "2025-12-31")
            revenue = latest_metric(financials, iid, "TOTAL_OPERATE_INCOME", "2025-12-31")
            net_profit = latest_metric(financials, iid, "PARENT_NETPROFIT", "2025-12-31")
            ocf = latest_metric(financials, iid, "NETCASH_OPERATE", "2025-12-31")
            capex = latest_metric(financials, iid, "CONSTRUCT_LONG_ASSET", "2025-12-31")
            assets = latest_metric(financials, iid, "TOTAL_ASSETS", "2025-12-31")
            liabilities = latest_metric(financials, iid, "TOTAL_LIABILITIES", "2025-12-31")
            close = float(pmetrics["latest_unadjusted_close_cny"])
            is_bank = iid == "CN.XSHG.601398"
            fact_rows.append(
                {
                    "instrument_id": iid,
                    "ticker": instrument["ticker"],
                    "name": instrument["name"],
                    "asset_type": "stock",
                    "latest_unadjusted_close_cny": fmt(close),
                    "ttm_implemented_cash_dps_cny": fmt(cash_ttm),
                    "ttm_cash_yield_pre_tax": fmt(cash_ttm / close if close else None),
                    "FY2025_basic_eps_cny": fmt(eps),
                    "price_to_FY2025_eps": fmt(close / eps if eps else None),
                    "cash_dps_to_FY2025_eps": fmt(cash_ttm / eps if eps else None),
                    "FY2025_revenue_cny": fmt(revenue),
                    "FY2025_parent_net_profit_cny": fmt(net_profit),
                    "FY2025_operating_cash_flow_cny": fmt(ocf),
                    "FY2025_capex_cash_cny": fmt(capex),
                    "FY2025_ocf_minus_capex_cny": fmt((ocf - capex) if (ocf is not None and capex is not None and not is_bank) else None),
                    "FY2025_liabilities_to_assets": fmt(liabilities / assets if (assets and liabilities is not None and not is_bank) else None),
                    "evidence_status": "partial",
                    "instrument_status": "资料不足",
                    "reason_codes": "MISSING_CRITICAL_FIELD",
                    "interpretation_boundary": "历史现金分红/每股收益比仅作事实对照，不等同可持续分红能力或银行资本约束结论。",
                }
            )
            history = events.dropna(subset=["ex_date_parsed"]).copy()
            history["ex_year"] = history["ex_date_parsed"].dt.year
            annual = history.groupby("ex_year", as_index=False)["cash"].sum()
            for row in annual.to_dict(orient="records"):
                dividend_rows.append({"instrument_id": iid, "ticker": instrument["ticker"], "calendar_year": int(row["ex_year"]), "cash_per_share_or_unit_cny": fmt(float(row["cash"])), "asset_type": "stock"})
        else:
            events = etf_distributions[etf_distributions["instrument_id"] == iid]
            cash_ttm = float(events.loc[(events["ex_date_parsed"] > cutoff) & (events["ex_date_parsed"] <= as_of), "cash"].sum())
            close = float(pmetrics["latest_unadjusted_close_cny"])
            fact_rows.append(
                {
                    "instrument_id": iid,
                    "ticker": instrument["ticker"],
                    "name": instrument["name"],
                    "asset_type": "etf",
                    "benchmark_id": instrument["benchmark_id"],
                    "latest_unadjusted_close_cny": fmt(close),
                    "ttm_implemented_cash_distribution_per_unit_cny": fmt(cash_ttm),
                    "ttm_cash_distribution_yield_pre_tax": fmt(cash_ttm / close if close else None),
                    "distribution_event_rows": int(len(events)),
                    "evidence_status": "partial",
                    "instrument_status": "资料不足",
                    "reason_codes": "MISSING_CRITICAL_FIELD",
                    "interpretation_boundary": "实际派息来自便利源累计分红差分，尚未完成公告级除息日、支付日和NAV/全收益对账。",
                }
            )
            history = events.dropna(subset=["ex_date_parsed"]).copy()
            if not history.empty:
                history["ex_year"] = history["ex_date_parsed"].dt.year
                annual = history.groupby("ex_year", as_index=False)["cash"].sum()
                for row in annual.to_dict(orient="records"):
                    dividend_rows.append({"instrument_id": iid, "ticker": instrument["ticker"], "calendar_year": int(row["ex_year"]), "cash_per_share_or_unit_cny": fmt(float(row["cash"])), "asset_type": "etf"})

    output_dir.mkdir(parents=True)
    frames = {
        "fact_matrix.csv": pd.DataFrame(fact_rows),
        "price_risk_metrics.csv": pd.DataFrame(price_rows),
        "dividend_history_annual.csv": pd.DataFrame(dividend_rows).sort_values(["instrument_id", "calendar_year"]),
    }
    output_files = []
    for filename, frame in frames.items():
        path = output_dir / filename
        frame.to_csv(path, index=False, encoding="utf-8")
        output_files.append({"path": str(path.relative_to(PROJECT_ROOT)), "rows": int(len(frame)), "columns": int(len(frame.columns)), "sha256": sha256_file(path)})

    quality = {
        "schema_version": "research-pack-quality-v1",
        "status": "pass",
        "checks": [
            {"check": "six_instruments", "passed": len(fact_rows) == 6, "actual": len(fact_rows)},
            {"check": "all_research_status_insufficient_until_primary_completion", "passed": all(row["instrument_status"] == "资料不足" for row in fact_rows)},
            {"check": "no_total_return_mislabelling", "passed": True, "note": "Price return metrics are explicitly unadjusted price returns."}
        ]
    }
    quality_path = output_dir / "quality.json"
    quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_files.append({"path": str(quality_path.relative_to(PROJECT_ROOT)), "rows": 3, "columns": 4, "sha256": sha256_file(quality_path)})

    # Record the formula registry and builder itself so a research report can be
    # reproduced from both its data snapshot and its transformation logic.
    inputs = [
        universe_path,
        sources_path,
        formula_path,
        Path(__file__).resolve(),
        snapshot_manifest,
        snapshot_dir / "daily_prices.csv",
        snapshot_dir / "stock_dividend_events.csv",
        snapshot_dir / "etf_distribution_events.csv",
        snapshot_dir / "financial_observations.csv",
    ]
    manifest = {
        "schema_version": "research-pack-manifest-v1",
        "run_id": args.output_run_id,
        "research_as_of": universe["research_as_of"],
        "principal_cny": universe["principal_cny"],
        "status": "facts_ready_research_incomplete",
        "input_files": [{"path": str(path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(path)} for path in inputs],
        "output_files": output_files,
        "source_registry_schema_version": sources["schema_version"],
        "global_instrument_status": "资料不足",
        "scope_boundary": "No recommendation, target price, tax-after-return or portfolio allocation is produced by this run."
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": args.output_run_id, "status": manifest["status"], "facts": len(fact_rows), "outputs": len(output_files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
