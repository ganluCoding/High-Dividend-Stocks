#!/usr/bin/env python3
"""Export the simple candidate table consumed by a future desktop UI."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "database" / "high_dividend.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "exports" / "candidate_pool_latest.csv"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit(f"Database is missing: {args.database}")

    sql = """
    SELECT
        ticker AS 代码,
        name AS 名称,
        asset_type AS 类型,
        category AS 类别,
        dividend_style AS 分红类型,
        risk_level AS 风险标签,
        data_status AS 数据状态,
        price_date AS 价格日期,
        latest_unadjusted_close_cny AS 最新未复权收盘价,
        trailing_12m_cash_per_unit_cny AS 近12个月已实施现金分配,
        trailing_12m_static_cash_yield_pre_tax AS 历史静态股息率_税前,
        CASE
            WHEN trailing_12m_static_cash_yield_pre_tax IS NULL
                THEN '未完成分配记录核验，不显示收益率'
            ELSE '近12个月已实施现金分配 ÷ 最新未复权收盘价；非前瞻预测，非总回报'
        END AS 口径说明
    FROM v_candidate_universe
    ORDER BY
        trailing_12m_static_cash_yield_pre_tax IS NULL,
        trailing_12m_static_cash_yield_pre_tax DESC,
        ticker
    """
    with sqlite3.connect(args.database) as conn:
        result = conn.execute(sql)
        columns = [item[0] for item in result.description]
        records = result.fetchall()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(records)
    print(f"exported {len(records)} candidates to {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
