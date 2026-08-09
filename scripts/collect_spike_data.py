#!/usr/bin/env python3
"""Collect the first internal-only data snapshot for the high-dividend PRD spike."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import akshare as ak
import baostock as bs
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = PROJECT_ROOT / "config" / "spike_universe.json"
ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_default(value: Any) -> str | None:
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return str(value)


def retry(label: str, fn: Callable[[], pd.DataFrame], attempts: int = 3) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
            if not isinstance(result, pd.DataFrame):
                raise TypeError(f"{label} did not return a DataFrame")
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    assert last_error is not None
    raise last_error


def collect_baostock_daily(
    instrument: dict[str, Any],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    provider_symbol = instrument["provider_symbol"].lower()
    bs_code = f"{provider_symbol[:2]}.{provider_symbol[2:]}"
    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        result = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,pctChg,isST",
            start_date=pd.Timestamp(start_date).strftime("%Y-%m-%d"),
            end_date=pd.Timestamp(end_date).strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",
        )
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        return pd.DataFrame(rows, columns=result.fields)
    finally:
        bs.logout()


def write_raw_dataframe(
    frame: pd.DataFrame,
    path: Path,
    *,
    dataset: str,
    instrument: dict[str, Any],
    observed_at: str,
    source_id: str,
    source_url: str,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    file_hash = sha256_file(path)
    meta_path = path.with_suffix(".meta.json")
    metadata = {
        "schema_version": "raw-envelope-v1",
        "dataset": dataset,
        "instrument_id": instrument["instrument_id"],
        "ticker": instrument["ticker"],
        "source_id": source_id,
        "source_url": source_url,
        "source_role": "convenience_collection_layer",
        "license_scope": "internal_research_only_pending_terms_verification",
        "observed_at": observed_at,
        "row_count": int(len(frame)),
        "column_count": int(len(frame.columns)),
        "columns": [str(column) for column in frame.columns],
        "content_sha256": file_hash,
        "data_file": path.name,
    }
    meta_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "meta_path": str(meta_path.relative_to(PROJECT_ROOT)),
        "sha256": file_hash,
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
        "dataset": dataset,
        "instrument_id": instrument["instrument_id"],
        "source_id": source_id,
    }


def filter_statement_as_of(
    frame: pd.DataFrame,
    start_date: str,
    research_as_of: pd.Timestamp,
) -> pd.DataFrame:
    result = frame.copy()
    if "REPORT_DATE" in result.columns:
        report_date = pd.to_datetime(result["REPORT_DATE"], errors="coerce", utc=True)
        start = pd.Timestamp(start_date, tz="Asia/Shanghai").tz_convert("UTC")
        result = result[report_date >= start]
    for column in ("NOTICE_DATE", "UPDATE_DATE"):
        if column in result.columns:
            values = pd.to_datetime(result[column], errors="coerce", utc=True)
            result = result[(values.isna()) | (values <= research_as_of.tz_convert("UTC"))]
    return result.reset_index(drop=True)


def normalize_prices(
    frame: pd.DataFrame,
    instrument: dict[str, Any],
    observed_at: str,
    raw_sha256: str,
    source_id: str,
) -> list[dict[str, Any]]:
    column_map = {
        "日期": "trade_date",
        "date": "trade_date",
        "开盘": "open",
        "open": "open",
        "收盘": "close",
        "close": "close",
        "最高": "high",
        "high": "high",
        "最低": "low",
        "low": "low",
        "成交量": "volume",
        "volume": "volume",
        "成交额": "turnover_cny",
        "amount": "turnover_cny",
        "振幅": "amplitude_pct",
        "涨跌幅": "change_pct",
        "涨跌额": "change_cny",
        "换手率": "turnover_rate_pct",
        "turn": "turnover_rate_pct",
        "turnover": "turnover_rate_fraction",
        "pctChg": "change_pct",
    }
    work = frame.rename(columns=column_map)
    records: list[dict[str, Any]] = []
    for row in work.to_dict(orient="records"):
        record = {
            "schema_version": "daily-price-v1",
            "instrument_id": instrument["instrument_id"],
            "ticker": instrument["ticker"],
            "asset_type": instrument["asset_type"],
            "source_id": source_id,
            "source_raw_sha256": raw_sha256,
            "observed_at": observed_at,
            "adjustment": "unadjusted",
        }
        for key in column_map.values():
            value = row.get(key)
            record[key] = None if pd.isna(value) or value == "" else str(value)
        if record.get("turnover_rate_pct") is None and record.get("turnover_rate_fraction") is not None:
            record["turnover_rate_pct"] = format(
                float(record["turnover_rate_fraction"]) * 100.0,
                ".10g",
            )
        record.pop("turnover_rate_fraction", None)
        records.append(record)
    return records


def normalize_stock_dividends(
    frame: pd.DataFrame,
    instrument: dict[str, Any],
    observed_at: str,
    raw_sha256: str,
    research_date: pd.Timestamp,
) -> list[dict[str, Any]]:
    work = frame.copy()
    for column in ("实施方案公告日期", "股权登记日", "除权日", "派息日", "股份到账日"):
        if column in work.columns:
            work[column] = pd.to_datetime(work[column], errors="coerce")
    if "实施方案公告日期" in work.columns:
        work = work[(work["实施方案公告日期"].isna()) | (work["实施方案公告日期"] <= research_date)]
    sort_columns = [column for column in ("报告时间", "实施方案公告日期", "除权日") if column in work.columns]
    if sort_columns:
        work = work.sort_values(sort_columns)
    if "报告时间" in work.columns:
        work["installment_no"] = work.groupby(["报告时间", "分红类型"], dropna=False).cumcount() + 1
    else:
        work["installment_no"] = range(1, len(work) + 1)

    records: list[dict[str, Any]] = []
    for row in work.to_dict(orient="records"):
        cash_per_10 = row.get("派息比例")
        dps = None if pd.isna(cash_per_10) else float(cash_per_10) / 10.0
        report_period = str(row.get("报告时间") or "unknown")
        distribution_type = str(row.get("分红类型") or "unknown")
        installment_no = int(row.get("installment_no") or 1)
        logical_key = "|".join(
            [instrument["instrument_id"], report_period, distribution_type, str(installment_no)]
        )
        event_id = sha256_text(logical_key)
        published_at = row.get("实施方案公告日期")
        published_text = None if pd.isna(published_at) else pd.Timestamp(published_at).date().isoformat()
        version_id = sha256_text(f"{event_id}|{published_text}|{raw_sha256}")
        ex_date = row.get("除权日")
        status = "implementation_announced"
        if not pd.isna(ex_date) and pd.Timestamp(ex_date) <= research_date:
            status = "implemented"
        records.append(
            {
                "schema_version": "dividend-event-v1",
                "instrument_id": instrument["instrument_id"],
                "ticker": instrument["ticker"],
                "dividend_event_id": event_id,
                "version_id": version_id,
                "supersedes_version_id": None,
                "profit_period_label": report_period,
                "distribution_type": distribution_type,
                "installment_no": str(installment_no),
                "status": status,
                "cash_per_10_shares_cny": None if cash_per_10 is None or pd.isna(cash_per_10) else str(cash_per_10),
                "cash_dps_cny": None if dps is None else format(dps, ".10g"),
                "published_at_date": published_text,
                "record_date": None if pd.isna(row.get("股权登记日")) else pd.Timestamp(row["股权登记日"]).date().isoformat(),
                "ex_date": None if pd.isna(ex_date) else pd.Timestamp(ex_date).date().isoformat(),
                "payment_date": None if pd.isna(row.get("派息日")) else pd.Timestamp(row["派息日"]).date().isoformat(),
                "description": None if pd.isna(row.get("实施方案分红说明")) else str(row.get("实施方案分红说明")),
                "source_id": "akshare_cninfo_convenience",
                "source_raw_sha256": raw_sha256,
                "observed_at": observed_at,
            }
        )
    return records


def normalize_etf_dividends(
    frame: pd.DataFrame,
    instrument: dict[str, Any],
    observed_at: str,
    raw_sha256: str,
    research_date: pd.Timestamp,
) -> list[dict[str, Any]]:
    if frame.empty or "日期" not in frame.columns or "累计分红" not in frame.columns:
        return []
    work = frame.copy()
    work["日期"] = pd.to_datetime(work["日期"], errors="coerce")
    work["累计分红"] = pd.to_numeric(work["累计分红"], errors="coerce")
    work = work.dropna(subset=["日期", "累计分红"]).sort_values("日期")
    work = work[work["日期"] <= research_date]
    work["本次分红"] = work["累计分红"].diff().fillna(work["累计分红"])
    records: list[dict[str, Any]] = []
    for row in work.to_dict(orient="records"):
        increment = float(row["本次分红"])
        if increment <= 1e-12:
            continue
        ex_date = pd.Timestamp(row["日期"]).date().isoformat()
        event_key = f"{instrument['instrument_id']}|{ex_date}|fund_distribution"
        event_id = sha256_text(event_key)
        records.append(
            {
                "schema_version": "etf-distribution-event-v1",
                "instrument_id": instrument["instrument_id"],
                "ticker": instrument["ticker"],
                "dividend_event_id": event_id,
                "version_id": sha256_text(f"{event_id}|{raw_sha256}"),
                "status": "implemented",
                "cash_per_unit_cny": format(increment, ".10g"),
                "cumulative_distribution_cny": format(float(row["累计分红"]), ".10g"),
                "ex_date": ex_date,
                "date_semantics": "provider_cumulative_dividend_date_assumed_ex_date_pending_primary_verification",
                "source_id": "akshare_sina_convenience",
                "source_raw_sha256": raw_sha256,
                "observed_at": observed_at,
            }
        )
    return records


def normalize_financial_observations(
    frame: pd.DataFrame,
    instrument: dict[str, Any],
    statement_type: str,
    observed_at: str,
    raw_sha256: str,
) -> list[dict[str, Any]]:
    metric_columns = {
        "profit": ["TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT", "NETPROFIT", "BASIC_EPS", "DILUTED_EPS"],
        "cash_flow": ["NETCASH_OPERATE", "CONSTRUCT_LONG_ASSET", "NETCASH_INVEST", "NETCASH_FINANCE"],
        "balance": ["TOTAL_ASSETS", "TOTAL_LIABILITIES", "PARENT_EQUITY", "TOTAL_EQUITY"],
    }[statement_type]
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        report_date = row.get("REPORT_DATE")
        notice_date = row.get("NOTICE_DATE")
        update_date = row.get("UPDATE_DATE")
        for metric in metric_columns:
            if metric not in row or pd.isna(row.get(metric)):
                continue
            records.append(
                {
                    "schema_version": "financial-observation-v1",
                    "instrument_id": instrument["instrument_id"],
                    "ticker": instrument["ticker"],
                    "statement_type": statement_type,
                    "metric": metric,
                    "value": str(row.get(metric)),
                    "currency": str(row.get("CURRENCY") or "CNY"),
                    "report_date": None if pd.isna(report_date) else pd.Timestamp(report_date).date().isoformat(),
                    "published_at_date": None if pd.isna(notice_date) else pd.Timestamp(notice_date).date().isoformat(),
                    "updated_at_date": None if pd.isna(update_date) else pd.Timestamp(update_date).date().isoformat(),
                    "source_id": "akshare_eastmoney_convenience",
                    "source_raw_sha256": raw_sha256,
                    "observed_at": observed_at,
                }
            )
    return records


def write_normalized(records: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records)
    if not frame.empty:
        sort_columns = [
            column
            for column in ("instrument_id", "trade_date", "report_date", "ex_date", "metric", "version_id")
            if column in frame.columns
        ]
        if sort_columns:
            frame = frame.sort_values(sort_columns)
    frame.to_csv(path, index=False, encoding="utf-8")
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "sha256": sha256_file(path),
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--run-id", default="run_20260809_initial")
    parser.add_argument("--instrument-id", action="append", dest="instrument_ids")
    parser.add_argument("--only-prices", action="store_true")
    parser.add_argument(
        "--skip-financials",
        action="store_true",
        help="Collect prices and dividend/distribution events only; useful for expanding a screening universe.",
    )
    parser.add_argument(
        "--price-provider",
        choices=("eastmoney", "sina", "tencent", "baostock"),
        default="eastmoney",
    )
    args = parser.parse_args()

    universe = json.loads(args.universe.read_text(encoding="utf-8"))
    research_as_of = pd.Timestamp(universe["research_as_of"])
    research_date = research_as_of.tz_localize(None).normalize()
    end_date = research_date.strftime("%Y%m%d")
    observed_at = datetime.now(ASIA_SHANGHAI).isoformat(timespec="seconds")
    raw_run_dir = PROJECT_ROOT / "data" / "raw" / args.run_id
    normalized_run_dir = PROJECT_ROOT / "data" / "normalized" / args.run_id
    manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{args.run_id}.json"
    if raw_run_dir.exists() or normalized_run_dir.exists() or manifest_path.exists():
        raise SystemExit(f"Run already exists and will not be overwritten: {args.run_id}")
    raw_run_dir.mkdir(parents=True)
    normalized_run_dir.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema_version": "research-run-manifest-v1",
        "run_id": args.run_id,
        "research_as_of": universe["research_as_of"],
        "observed_at": observed_at,
        "principal_cny": universe["principal_cny"],
        "universe_schema_version": universe["schema_version"],
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "source_role": "convenience_collection_layer_pending_primary_verification",
        "license_scope": "internal_research_only",
        "raw_files": [],
        "normalized_files": [],
        "failures": [],
    }

    normalized_prices: list[dict[str, Any]] = []
    normalized_stock_dividends: list[dict[str, Any]] = []
    normalized_etf_dividends: list[dict[str, Any]] = []
    normalized_financials: list[dict[str, Any]] = []

    warnings.filterwarnings("ignore", category=Warning)
    instruments = universe["instruments"]
    if args.instrument_ids:
        selected = set(args.instrument_ids)
        instruments = [item for item in instruments if item["instrument_id"] in selected]
        missing = selected - {item["instrument_id"] for item in instruments}
        if missing:
            raise SystemExit(f"Unknown instrument_id(s): {', '.join(sorted(missing))}")

    for instrument in instruments:
        instrument_dir = raw_run_dir / instrument["instrument_id"]
        start_date = instrument["start_date"].replace("-", "")
        print(f"[{instrument['ticker']}] prices", flush=True)
        try:
            if args.price_provider == "baostock":
                if instrument["asset_type"] != "stock":
                    raise ValueError("BaoStock fallback is only configured for A-share stocks")
                price_frame = retry(
                    "stock_daily_ohlcv_baostock",
                    lambda i=instrument: collect_baostock_daily(i, start_date, end_date),
                )
                price_source_id = "baostock_convenience"
                price_source_url = "http://baostock.com/baostock/index.php/Python_API%E6%96%87%E6%A1%A3"
            elif args.price_provider == "tencent":
                if instrument["asset_type"] != "stock":
                    raise ValueError("Tencent fallback is only configured for A-share stocks")
                price_frame = retry(
                    "stock_daily_ohlcv_tencent",
                    lambda i=instrument: ak.stock_zh_a_hist_tx(
                        symbol=i["provider_symbol"],
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                    ),
                )
                # Tencent's `amount` field in this endpoint is trading volume in board lots.
                price_frame = price_frame.rename(columns={"amount": "volume"})
                price_frame["volume"] = pd.to_numeric(
                    price_frame["volume"], errors="coerce"
                ) * 100.0
                price_source_id = "akshare_tencent_convenience"
                price_source_url = f"https://gu.qq.com/{instrument['provider_symbol'].lower()}/gp"
            elif args.price_provider == "sina" and instrument["asset_type"] == "stock":
                price_frame = retry(
                    "stock_daily_ohlcv_sina",
                    lambda i=instrument: ak.stock_zh_a_daily(
                        symbol=i["provider_symbol"],
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                    ),
                )
                price_source_id = "akshare_sina_convenience"
                price_source_url = (
                    "https://finance.sina.com.cn/realstock/company/"
                    f"{instrument['provider_symbol'].lower()}/nc.shtml"
                )
            elif args.price_provider == "sina":
                price_frame = retry(
                    "etf_daily_ohlcv_sina",
                    lambda i=instrument: ak.fund_etf_hist_sina(symbol=i["provider_symbol"]),
                )
                price_dates = pd.to_datetime(price_frame.get("date"), errors="coerce")
                price_frame = price_frame[
                    (price_dates >= pd.Timestamp(instrument["start_date"]))
                    & (price_dates <= research_date)
                ].reset_index(drop=True)
                price_source_id = "akshare_sina_convenience"
                price_source_url = (
                    f"https://finance.sina.com.cn/fund/quotes/{instrument['code']}/bc.shtml"
                )
            elif instrument["asset_type"] == "stock":
                price_frame = retry(
                    "stock_daily_ohlcv",
                    lambda i=instrument: ak.stock_zh_a_hist(
                        symbol=i["code"],
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                    ),
                )
                price_source_url = (
                    f"https://quote.eastmoney.com/{instrument['provider_symbol'].lower()}.html"
                )
                price_source_id = "akshare_eastmoney_convenience"
            else:
                price_frame = retry(
                    "etf_daily_ohlcv",
                    lambda i=instrument: ak.fund_etf_hist_em(
                        symbol=i["code"],
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust="",
                    ),
                )
                price_source_url = f"https://quote.eastmoney.com/sh{instrument['code']}.html"
                price_source_id = "akshare_eastmoney_convenience"
            raw_entry = write_raw_dataframe(
                price_frame,
                instrument_dir / "daily_ohlcv.csv",
                dataset="daily_ohlcv",
                instrument=instrument,
                observed_at=observed_at,
                source_id=price_source_id,
                source_url=price_source_url,
            )
            manifest["raw_files"].append(raw_entry)
            normalized_prices.extend(
                normalize_prices(
                    price_frame,
                    instrument,
                    observed_at,
                    raw_entry["sha256"],
                    price_source_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            manifest["failures"].append(
                {"instrument_id": instrument["instrument_id"], "dataset": "daily_ohlcv", "error": str(exc)}
            )

        if args.only_prices:
            continue

        if instrument["asset_type"] == "stock":
            print(f"[{instrument['ticker']}] dividends", flush=True)
            try:
                dividend_frame = retry(
                    "stock_dividend_implemented",
                    lambda i=instrument: ak.stock_dividend_cninfo(symbol=i["code"]),
                )
                raw_entry = write_raw_dataframe(
                    dividend_frame,
                    instrument_dir / "stock_dividends_implemented.csv",
                    dataset="stock_dividends_implemented",
                    instrument=instrument,
                    observed_at=observed_at,
                    source_id="akshare_cninfo_convenience",
                    source_url="https://webapi.cninfo.com.cn/",
                )
                manifest["raw_files"].append(raw_entry)
                normalized_stock_dividends.extend(
                    normalize_stock_dividends(
                        dividend_frame,
                        instrument,
                        observed_at,
                        raw_entry["sha256"],
                        research_date,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                manifest["failures"].append(
                    {"instrument_id": instrument["instrument_id"], "dataset": "stock_dividends_implemented", "error": str(exc)}
                )

            if args.skip_financials:
                continue

            statement_calls = {
                "profit": ak.stock_profit_sheet_by_report_em,
                "cash_flow": ak.stock_cash_flow_sheet_by_report_em,
                "balance": ak.stock_balance_sheet_by_report_em,
            }
            for statement_type, function in statement_calls.items():
                print(f"[{instrument['ticker']}] {statement_type}", flush=True)
                try:
                    statement_frame = retry(
                        statement_type,
                        lambda f=function, i=instrument: f(symbol=i["provider_symbol"]),
                    )
                    statement_frame = filter_statement_as_of(
                        statement_frame,
                        instrument["start_date"],
                        research_as_of,
                    )
                    raw_entry = write_raw_dataframe(
                        statement_frame,
                        instrument_dir / f"financial_{statement_type}.csv",
                        dataset=f"financial_{statement_type}",
                        instrument=instrument,
                        observed_at=observed_at,
                        source_id="akshare_eastmoney_convenience",
                        source_url=(
                            "https://emweb.securities.eastmoney.com/PC_HSF10/"
                            "NewFinanceAnalysis/Index?type=web&code="
                            f"{instrument['provider_symbol'].lower()}"
                        ),
                    )
                    manifest["raw_files"].append(raw_entry)
                    normalized_financials.extend(
                        normalize_financial_observations(
                            statement_frame,
                            instrument,
                            statement_type,
                            observed_at,
                            raw_entry["sha256"],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    manifest["failures"].append(
                        {"instrument_id": instrument["instrument_id"], "dataset": f"financial_{statement_type}", "error": str(exc)}
                    )
        else:
            print(f"[{instrument['ticker']}] ETF distributions", flush=True)
            try:
                distribution_frame = retry(
                    "etf_distribution_history",
                    lambda i=instrument: ak.fund_etf_dividend_sina(symbol=i["provider_symbol"]),
                )
                raw_entry = write_raw_dataframe(
                    distribution_frame,
                    instrument_dir / "etf_distributions_cumulative.csv",
                    dataset="etf_distributions_cumulative",
                    instrument=instrument,
                    observed_at=observed_at,
                    source_id="akshare_sina_convenience",
                    source_url=f"https://finance.sina.com.cn/fund/quotes/{instrument['code']}/bc.shtml",
                )
                manifest["raw_files"].append(raw_entry)
                normalized_etf_dividends.extend(
                    normalize_etf_dividends(
                        distribution_frame,
                        instrument,
                        observed_at,
                        raw_entry["sha256"],
                        research_date,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                manifest["failures"].append(
                    {"instrument_id": instrument["instrument_id"], "dataset": "etf_distributions_cumulative", "error": str(exc)}
                )

    normalized_outputs = {
        "daily_prices.csv": normalized_prices,
        "stock_dividend_events.csv": normalized_stock_dividends,
        "etf_distribution_events.csv": normalized_etf_dividends,
        "financial_observations.csv": normalized_financials,
    }
    for filename, records in normalized_outputs.items():
        entry = write_normalized(records, normalized_run_dir / filename)
        manifest["normalized_files"].append(entry)

    all_hashes = sorted(
        [entry["sha256"] for entry in manifest["raw_files"]]
        + [entry["sha256"] for entry in manifest["normalized_files"]]
    )
    manifest["input_manifest_hash"] = sha256_text("\n".join(all_hashes))
    manifest["status"] = "complete_with_failures" if manifest["failures"] else "complete"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "run_id": args.run_id,
        "status": manifest["status"],
        "raw_file_count": len(manifest["raw_files"]),
        "normalized_file_count": len(manifest["normalized_files"]),
        "failure_count": len(manifest["failures"]),
        "manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
        "input_manifest_hash": manifest["input_manifest_hash"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
