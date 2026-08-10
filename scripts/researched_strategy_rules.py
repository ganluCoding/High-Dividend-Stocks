#!/usr/bin/env python3
"""Public-methodology-inspired stock screens backed by one immutable release.

These are transparent research proxies, not licensed index replications.  The
payload records the known gaps so a high rank cannot be mistaken for an index
constituent claim or an investment recommendation.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any


ORDINARY_DIVIDEND_EXCLUSIONS = {"特别分红", "股改分红"}


def _previous_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _latest_prices(connection: sqlite3.Connection, cutoff: str) -> dict[str, tuple[str, float]]:
    rows = connection.execute(
        """WITH ranked AS (
               SELECT instrument_id, trade_date, close,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument_id
                          ORDER BY trade_date DESC, run_id DESC
                      ) AS rn
               FROM market_daily_prices
               WHERE validation_status='approved' AND trade_date<=?
           )
           SELECT instrument_id, trade_date, close FROM ranked WHERE rn=1""",
        (cutoff,),
    )
    return {str(row[0]): (str(row[1]), float(row[2])) for row in rows}


def _ordinary_dividends(
    connection: sqlite3.Connection, cutoff: str, prices: dict[str, tuple[str, float]]
) -> tuple[dict[str, dict[int, float]], dict[str, float], dict[str, int]]:
    annual: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    ttm: dict[str, float] = defaultdict(float)
    event_years: dict[str, set[str]] = defaultdict(set)
    rows = connection.execute(
        """WITH ranked AS (
               SELECT d.*,
                      ROW_NUMBER() OVER (
                          PARTITION BY dividend_event_id
                          ORDER BY CASE WHEN status='implemented' THEN 1 ELSE 0 END DESC,
                                   COALESCE(observed_at, '') DESC,
                                   run_id DESC, version_id DESC
                      ) AS rn
               FROM stock_dividend_events d
           )
           SELECT instrument_id, profit_period_label, distribution_type, ex_date, cash_dps_cny
           FROM ranked
           WHERE rn=1 AND status='implemented' AND ex_date IS NOT NULL
             AND cash_dps_cny>0
             AND COALESCE(published_at_date, substr(observed_at, 1, 10), '9999-12-31')<=?""",
        (cutoff,),
    )
    for instrument_id, period_label, distribution_type, ex_date, cash_dps in rows:
        instrument_id = str(instrument_id)
        if str(distribution_type or "") in ORDINARY_DIVIDEND_EXCLUSIONS:
            continue
        ex_day = date.fromisoformat(str(ex_date)[:10])
        amount = float(cash_dps)
        event_years[instrument_id].add(str(ex_date)[:4])
        label = str(period_label or "")
        if len(label) >= 4 and label[:4].isdigit():
            annual[instrument_id][int(label[:4])] += amount
        price = prices.get(instrument_id)
        if price is not None:
            price_day = date.fromisoformat(price[0][:10])
            if _previous_year(price_day) < ex_day <= price_day:
                ttm[instrument_id] += amount
    return annual, dict(ttm), {key: len(value) for key, value in event_years.items()}


def _annual_eps(connection: sqlite3.Connection, cutoff: str) -> dict[str, dict[int, float]]:
    values: dict[str, dict[int, float]] = defaultdict(dict)
    rows = connection.execute(
        """WITH ranked AS (
               SELECT instrument_id, report_date, value,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument_id, metric, report_date
                          ORDER BY COALESCE(updated_at_date, '') DESC,
                                   COALESCE(observed_at, '') DESC, run_id DESC
                      ) AS rn
               FROM financial_observations
               WHERE metric='BASIC_EPS'
                 AND COALESCE(published_at_date, substr(observed_at, 1, 10), '9999-12-31')<=?
           )
           SELECT instrument_id, report_date, value
           FROM ranked
           WHERE rn=1 AND substr(report_date, 6, 5)='12-31' AND value IS NOT NULL""",
        (cutoff,),
    )
    for instrument_id, report_date, value in rows:
        values[str(instrument_id)][int(str(report_date)[:4])] = float(value)
    return values


def _year_end_closes(
    connection: sqlite3.Connection, first_year: int, last_year: int
) -> dict[str, dict[int, float]]:
    values: dict[str, dict[int, float]] = defaultdict(dict)
    rows = connection.execute(
        """WITH canonical AS (
               SELECT instrument_id, trade_date, close,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument_id, trade_date
                          ORDER BY COALESCE(observed_at, '') DESC, run_id DESC, source_id DESC
                      ) AS rn
               FROM price_daily
               WHERE adjustment='unadjusted' AND trade_date BETWEEN ? AND ?
           ), year_ranked AS (
               SELECT instrument_id, substr(trade_date, 1, 4) AS trade_year, close,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument_id, substr(trade_date, 1, 4)
                          ORDER BY trade_date DESC
                      ) AS rn
               FROM canonical WHERE rn=1
           )
           SELECT instrument_id, trade_year, close FROM year_ranked WHERE rn=1""",
        (f"{first_year}-01-01", f"{last_year}-12-31"),
    )
    for instrument_id, trade_year, close in rows:
        values[str(instrument_id)][int(trade_year)] = float(close)
    return values


def _daily_history(
    connection: sqlite3.Connection, cutoff: str, lookback_days: int = 370
) -> dict[str, list[tuple[date, float, float | None]]]:
    cutoff_day = date.fromisoformat(cutoff[:10])
    start = cutoff_day - timedelta(days=lookback_days)
    values: dict[str, list[tuple[date, float, float | None]]] = defaultdict(list)
    rows = connection.execute(
        """WITH canonical AS (
               SELECT instrument_id, trade_date, close, turnover_cny,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument_id, trade_date
                          ORDER BY COALESCE(observed_at, '') DESC, run_id DESC, source_id DESC
                      ) AS rn
               FROM price_daily
               WHERE adjustment='unadjusted' AND trade_date>? AND trade_date<=?
           )
           SELECT instrument_id, trade_date, close, turnover_cny
           FROM canonical WHERE rn=1 ORDER BY instrument_id, trade_date""",
        (start.isoformat(), cutoff[:10]),
    )
    for instrument_id, trade_date, close, turnover in rows:
        values[str(instrument_id)].append(
            (date.fromisoformat(str(trade_date)[:10]), float(close), None if turnover is None else float(turnover))
        )
    return values


def _average_turnover(rows: list[tuple[date, float, float | None]], start: date) -> float | None:
    values = [turnover for trade_day, _, turnover in rows if trade_day > start and turnover is not None]
    return None if not values else sum(values) / len(values)


def _volatility(rows: list[tuple[date, float, float | None]]) -> float | None:
    closes = [row[1] for row in rows]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)) if closes[index - 1] > 0]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(252)


def _base_payload(
    instrument_id: str,
    ticker: str,
    name: str,
    prices: dict[str, tuple[str, float]],
    ttm_cash: dict[str, float],
    history_years: dict[str, int],
) -> dict[str, Any]:
    price = prices.get(instrument_id)
    close = None if price is None else price[1]
    ttm_yield = None if not close else ttm_cash.get(instrument_id, 0.0) / close
    return {
        "ticker": ticker,
        "name": name,
        "price_date": None if price is None else price[0],
        "reference_close_cny": close,
        "ordinary_dividend_history_years": history_years.get(instrument_id, 0),
        "ordinary_ttm_cash_yield_pre_tax": ttm_yield,
        "boundary": "当前时点的历史税前现金分红研究筛选，不是未来股息率、指数成分预测或买入建议。",
    }


def _is_st(name: str) -> bool:
    return "ST" in name.upper()


def run_researched_stock_rule(
    connection: sqlite3.Connection, rule: dict[str, Any], available_cutoff: str
) -> list[dict[str, Any]]:
    """Run one of the public-methodology-inspired current-snapshot screens."""
    cutoff = available_cutoff[:10]
    instruments = [tuple(map(str, row)) for row in connection.execute(
        "SELECT instrument_id, ticker, name FROM security_master WHERE asset_type='stock' ORDER BY instrument_id"
    )]
    prices = _latest_prices(connection, cutoff)
    annual_dps, ttm_cash, history_years = _ordinary_dividends(connection, cutoff, prices)
    eps = _annual_eps(connection, cutoff)
    base = {
        instrument_id: _base_payload(instrument_id, ticker, name, prices, ttm_cash, history_years)
        for instrument_id, ticker, name in instruments
    }
    rule_id = rule["rule_id"]
    if rule_id == "sse_dividend_quality_proxy":
        return _run_sse_proxy(connection, rule, instruments, prices, annual_dps, eps, base)
    if rule_id == "china_high_dividend_low_vol_proxy":
        return _run_low_vol_proxy(connection, rule, instruments, prices, ttm_cash, base, cutoff)
    if rule_id == "china_dividend_opportunity_proxy":
        return _run_opportunity_proxy(connection, rule, instruments, prices, annual_dps, eps, ttm_cash, base, cutoff)
    raise ValueError(f"Unsupported researched rule id: {rule_id}")


def _result(
    instrument_id: str, state: str, reasons: list[str], priority: int | None, payload: dict[str, Any]
) -> dict[str, Any]:
    return {"instrument_id": instrument_id, "state": state, "reasons": reasons, "priority": priority, "payload": payload}


def _run_sse_proxy(
    connection: sqlite3.Connection,
    rule: dict[str, Any],
    instruments: list[tuple[str, str, str]],
    prices: dict[str, tuple[str, float]],
    annual_dps: dict[str, dict[int, float]],
    eps: dict[str, dict[int, float]],
    base: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    target = int(params["target_count"])
    latest_year = max(date.fromisoformat(value[0]).year for value in prices.values()) - 1
    years = list(range(latest_year - int(params["dividend_years"]) + 1, latest_year + 1))
    year_closes = _year_end_closes(connection, years[0], years[-1])
    eligible: list[tuple[float, str, list[float], list[float]]] = []
    status: dict[str, tuple[str, list[str]]] = {}
    for instrument_id, _, name in instruments:
        annual = annual_dps.get(instrument_id, {})
        annual_eps = eps.get(instrument_id, {})
        closes = year_closes.get(instrument_id, {})
        if _is_st(name):
            status[instrument_id] = ("不纳入本模板", ["ST_EXCLUDED"])
        elif not all(annual.get(year, 0) > 0 for year in years):
            status[instrument_id] = ("不纳入本模板", ["DIVIDEND_NOT_CONTINUOUS"])
        elif not all(year in annual_eps and annual_eps[year] > 0 for year in years):
            status[instrument_id] = ("资料不足", ["MISSING_ANNUAL_EPS"])
        elif not all(year in closes and closes[year] > 0 for year in years):
            status[instrument_id] = ("资料不足", ["MISSING_THREE_YEAR_PRICES"])
        else:
            payout = [annual[year] / annual_eps[year] for year in years]
            average_payout = sum(payout) / len(payout)
            if not (0 < average_payout < 1 and 0 < payout[-1] < 1):
                status[instrument_id] = ("不纳入本模板", ["PAYOUT_OUTSIDE_RANGE"])
                continue
            annual_yields = [annual[year] / closes[year] for year in years]
            average_yield = sum(annual_yields) / len(annual_yields)
            eligible.append((average_yield, instrument_id, payout, annual_yields))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    selected = {item[1]: (rank, item) for rank, item in enumerate(eligible[:target], start=1)}
    for _, instrument_id, _, _ in eligible[target:]:
        status[instrument_id] = ("不纳入本模板", ["RANK_BELOW_CUTOFF"])
    results = []
    for instrument_id, _, _ in instruments:
        payload = dict(base[instrument_id])
        payload.update({
            "method": "上证红利公开方法研究代理",
            "fiscal_years": years,
            "replication_status": "非指数复刻：缺少全市场历史总市值/自由流通市值及官方样本空间。",
        })
        if instrument_id in selected:
            rank, item = selected[instrument_id]
            payload.update({
                "research_rank": rank,
                "average_dividend_yield_3y": item[0],
                "annual_payout_ratios": item[2],
                "annual_dividend_yields": item[3],
            })
            results.append(_result(instrument_id, "资料足以研究", ["PASS"], rank, payload))
        else:
            state, reasons = status.get(instrument_id, ("资料不足", ["MISSING_PRICE_OR_DIVIDEND_FACTS"]))
            results.append(_result(instrument_id, state, reasons, None, payload))
    return results


def _run_low_vol_proxy(
    connection: sqlite3.Connection,
    rule: dict[str, Any],
    instruments: list[tuple[str, str, str]],
    prices: dict[str, tuple[str, float]],
    ttm_cash: dict[str, float],
    base: dict[str, dict[str, Any]],
    cutoff: str,
) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    target = int(params["target_count"])
    shortlist_size = int(params["yield_shortlist_count"])
    minimum_days = int(params["minimum_trading_days"])
    minimum_advt = float(params["minimum_advt_cny"])
    rows = _daily_history(connection, cutoff)
    cutoff_day = date.fromisoformat(cutoff)
    advt_start = cutoff_day - timedelta(days=int(params["advt_calendar_days"]))
    eligible: list[tuple[float, str, float, float]] = []
    status: dict[str, tuple[str, list[str]]] = {}
    for instrument_id, _, name in instruments:
        price = prices.get(instrument_id)
        history = rows.get(instrument_id, [])
        one_year_history = [row for row in history if row[0] > _previous_year(cutoff_day)]
        if _is_st(name):
            status[instrument_id] = ("不纳入本模板", ["ST_EXCLUDED"])
        elif price is None or len(one_year_history) < minimum_days:
            status[instrument_id] = ("资料不足", ["MISSING_ONE_YEAR_PRICE_HISTORY"])
        else:
            yield_value = ttm_cash.get(instrument_id, 0.0) / price[1]
            advt = _average_turnover(history, advt_start)
            volatility = _volatility(one_year_history)
            if yield_value <= 0:
                status[instrument_id] = ("不纳入本模板", ["NO_ORDINARY_TTM_DIVIDEND"])
            elif advt is None:
                status[instrument_id] = ("资料不足", ["MISSING_TURNOVER"])
            elif advt < minimum_advt:
                status[instrument_id] = ("不纳入本模板", ["LIQUIDITY_BELOW_ENTRY"])
            elif volatility is None:
                status[instrument_id] = ("资料不足", ["MISSING_VOLATILITY"])
            else:
                eligible.append((yield_value, instrument_id, volatility, advt))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    shortlist = eligible[:shortlist_size]
    for _, instrument_id, _, _ in eligible[shortlist_size:]:
        status[instrument_id] = ("不纳入本模板", ["YIELD_RANK_BELOW_SHORTLIST"])
    shortlist.sort(key=lambda item: (item[2], item[1]))
    selected = {item[1]: (rank, item) for rank, item in enumerate(shortlist[:target], start=1)}
    for _, instrument_id, _, _ in shortlist[target:]:
        status[instrument_id] = ("不纳入本模板", ["VOLATILITY_RANK_BELOW_CUTOFF"])
    results = []
    for instrument_id, _, _ in instruments:
        payload = dict(base[instrument_id])
        payload.update({
            "method": "标普中国A股高股息低波公开方法研究代理",
            "replication_status": "非指数复刻：缺少自由流通市值、GICS行业上限和历史成分缓冲。",
        })
        if instrument_id in selected:
            rank, item = selected[instrument_id]
            payload.update({
                "research_rank": rank,
                "annualized_price_volatility_1y": item[2],
                "average_daily_turnover_cny": item[3],
            })
            results.append(_result(instrument_id, "资料足以研究", ["PASS"], rank, payload))
        else:
            state, reasons = status.get(instrument_id, ("资料不足", ["MISSING_PRICE_OR_DIVIDEND_FACTS"]))
            results.append(_result(instrument_id, state, reasons, None, payload))
    return results


def _run_opportunity_proxy(
    connection: sqlite3.Connection,
    rule: dict[str, Any],
    instruments: list[tuple[str, str, str]],
    prices: dict[str, tuple[str, float]],
    annual_dps: dict[str, dict[int, float]],
    eps: dict[str, dict[int, float]],
    ttm_cash: dict[str, float],
    base: dict[str, dict[str, Any]],
    cutoff: str,
) -> list[dict[str, Any]]:
    params = rule["required_parameters"]
    target = int(params["target_count"])
    latest_year = max(date.fromisoformat(value[0]).year for value in prices.values()) - 1
    years = [latest_year - 1, latest_year]
    minimum_advt = float(params["minimum_advt_cny"])
    minimum_days = int(params["minimum_trading_days"])
    rows = _daily_history(connection, cutoff)
    advt_start = date.fromisoformat(cutoff) - timedelta(days=int(params["advt_calendar_days"]))
    eligible: list[tuple[float, str, float, float]] = []
    status: dict[str, tuple[str, list[str]]] = {}
    for instrument_id, _, name in instruments:
        annual = annual_dps.get(instrument_id, {})
        price = prices.get(instrument_id)
        latest_eps = eps.get(instrument_id, {}).get(latest_year)
        if _is_st(name):
            status[instrument_id] = ("不纳入本模板", ["ST_EXCLUDED"])
        elif price is None:
            status[instrument_id] = ("资料不足", ["MISSING_CURRENT_PRICE"])
        elif not all(annual.get(year, 0) > 0 for year in years):
            status[instrument_id] = ("不纳入本模板", ["DIVIDEND_PAYMENT_HISTORY_SHORT"])
        elif latest_eps is None:
            status[instrument_id] = ("资料不足", ["MISSING_ANNUAL_EPS"])
        elif latest_eps <= 0 or not (0 < annual[latest_year] / latest_eps < 1):
            status[instrument_id] = ("不纳入本模板", ["PAYOUT_OUTSIDE_RANGE"])
        else:
            history = rows.get(instrument_id, [])
            recent_history = [row for row in history if row[0] > advt_start]
            advt = _average_turnover(history, advt_start)
            yield_value = ttm_cash.get(instrument_id, 0.0) / price[1]
            if len(recent_history) < minimum_days:
                status[instrument_id] = ("资料不足", ["MISSING_SIX_MONTH_PRICE_HISTORY"])
            elif advt is None:
                status[instrument_id] = ("资料不足", ["MISSING_TURNOVER"])
            elif advt < minimum_advt:
                status[instrument_id] = ("不纳入本模板", ["LIQUIDITY_BELOW_ENTRY"])
            elif yield_value <= 0:
                status[instrument_id] = ("不纳入本模板", ["NO_ORDINARY_TTM_DIVIDEND"])
            else:
                eligible.append((yield_value, instrument_id, advt, annual[latest_year] / latest_eps))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    selected = {item[1]: (rank, item) for rank, item in enumerate(eligible[:target], start=1)}
    for _, instrument_id, _, _ in eligible[target:]:
        status[instrument_id] = ("不纳入本模板", ["RANK_BELOW_CUTOFF"])
    results = []
    for instrument_id, _, _ in instruments:
        payload = dict(base[instrument_id])
        payload.update({
            "method": "标普中国A股红利机会公开方法研究代理",
            "fiscal_years": years,
            "replication_status": "非指数复刻：缺少自由流通市值、GICS权重约束、官方成分缓冲及逐股一级来源确认。",
        })
        if instrument_id in selected:
            rank, item = selected[instrument_id]
            payload.update({
                "research_rank": rank,
                "average_daily_turnover_cny": item[2],
                "latest_payout_ratio": item[3],
            })
            results.append(_result(instrument_id, "资料足以研究", ["PASS"], rank, payload))
        else:
            state, reasons = status.get(instrument_id, ("资料不足", ["MISSING_PRICE_OR_DIVIDEND_FACTS"]))
            results.append(_result(instrument_id, state, reasons, None, payload))
    return results
