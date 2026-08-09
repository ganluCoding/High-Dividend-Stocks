# 数据库 v1（运行时首版已发布）

数据库文件：运行时正式库为 `~/Library/Application Support/HighDividend/data/database/high_dividend.db`（SQLite）；工作区 `data/database/high_dividend.db` 是开发种子库。它是后续策略库和桌面端的**查询层**；原始和规范化CSV仍保留为不可改写的数据底稿。

## 已有内容

- 全市场事实主表：5,422只股票、1,603只ETF；核心标的池仍按银行、电力、能源、通信、航运等类别维护；
- 日行情：未复权OHLCV和成交额；
- 已实施的股票分红与ETF分配事件；
- 财报指标；
- 数据来源、采集运行和快照hash。

## 直接可用的两个视图

```sql
SELECT * FROM v_static_cash_yield
ORDER BY trailing_12m_static_cash_yield_pre_tax DESC;

SELECT * FROM v_latest_unadjusted_close;
```

`v_static_cash_yield` 的定义是“过去12个月已实施现金分配 ÷ 最新未复权收盘价”，税前且只作历史静态参考；它不是预测股息率、总回报或买入信号。未完成分配记录采集或核验的标的，不会产生数值型收益率，避免制造虚假的0%或空白结论。

面向后续桌面端的简单候选表可由下列命令导出：

```bash
python3 scripts/export_candidate_pool.py
```

输出为 `data/exports/candidate_pool_latest.csv`。没有分配事件的ETF会显示空收益率，而不是0%。

## 更新方式

本机已由两个用户级 `launchd` 任务维护：每日任务每小时唤醒检查收盘日行情，每周任务刷新核心分红、ETF分配和财务事实。失败批次进入隔离状态，不替换最后一个已发布版本。查看状态：

```bash
launchctl print gui/$(id -u)/com.highdividend.daily-update
launchctl print gui/$(id -u)/com.highdividend.low-frequency-update
```

构建或重建数据库：

```bash
python3 scripts/build_database.py
```

当明确需要以新快照替换现有数据库时：

```bash
python3 scripts/build_database.py --snapshot-run <run_id> --replace
```

数据库不直接抓取网络数据。新的采集先生成可审计快照，再导入数据库；这样策略和桌面端不会读取半截或来源不明的数据。
