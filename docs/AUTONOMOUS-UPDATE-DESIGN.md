# 本地自治数据更新设计 v2（已部署，2026-08-09验收）

## 当前发布状态

运行时数据库已部署到 `~/Library/Application Support/HighDividend/data/database/high_dividend.db`，并通过首版验收：5,422只股票、1,603只ETF的行情快照，31只核心股票分红、4只ETF分配、31只核心股票财务摘要均已写入已发布版本。候选配置仍包含5只核心ETF，缺失的低频事实会在覆盖矩阵中明确显示。工作区中的 `data/database/high_dividend.db` 仍是开发种子库；本机定时任务读取运行时数据库。

日任务使用 BaoStock 全量证券清单、腾讯批量行情和 BaoStock 40只抽样复核；低频任务每周运行，使用 CNINFO 分红、Sina ETF 累计分配和 BaoStock 财务事实。便利源仅用于本机内部研究，未授权原始数据对外再分发。

## 目标

让本机在没有Codex参与的情况下，持续维护高股息研究数据：自动判断交易日、抓取增量数据、校验、发布或隔离失败批次，并让策略库与桌面端只读取已发布的SQLite数据库。

本设计不自动下单、不发送交易指令。安装 `launchd` 后，仍可完全在本机停止、查看日志、回滚和重建。

## 历史原型（已被自治运行时替代）

此前是手动原型：

```text
手动运行采集脚本
  → raw/<run> 原始CSV与hash
  → normalized/<run> 规范化CSV
  → curated/<run> 冻结快照
  → high_dividend.db 全量重建
```

优点是快照、来源和hash可追溯；上述缺口已由自治运行时补上。每日行情和每周低频任务都采用暂存、覆盖率/日期/跨源抽样门禁、事务发布、备份和失败隔离。

## 目标架构

```text
launchd（本机用户任务，每小时唤醒检查）
  → daily_update.py：交易日与水位线判断、单实例锁
  → Source Adapters：许可的价格/分红/ETF/公告数据源
  → raw/staging/<run_id>：原始响应、元数据、hash
  → validate_run.py：质量门禁与失败隔离
  → build_release.py：生成 high_dividend.next.db
  → 原子替换 high_dividend.db + releases 备份 + health.json
  → 桌面端、策略库：只读已发布 high_dividend.db
```

### 每次成功更新的流程

1. 检查本地交易日历、上次成功水位线、网络和单实例锁。
2. 从“上次成功交易日向前回看5个交易日”增量抓取至最近已收盘交易日；重叠窗口用于修正迟到或更正的数据。
3. 将原始结果写入唯一运行目录，不覆盖历史运行。
4. 执行质量门禁：hash、字段、日期、OHLC逻辑、价格异常、覆盖率、分红重复、数据陈旧度和源失败率。
5. 仅通过门禁的数据进入 `approved`；不通过的批次进入 `quarantine`，不触碰正式库。
6. 在临时SQLite文件中事务性合并，运行完整性和冒烟查询。
7. 备份当前库和发布清单，再原子替换正式库；更新 `health.json`。

失败时不发布新库。下一次运行会自动补采；桌面端继续使用最后一次成功版本，并显示“最后成功时间、数据日期、失败标的和数据源”。

## macOS定时机制

使用 `launchd` 的用户级 LaunchAgent，不用 cron。

- `RunAtLoad=true`：电脑重启/登录后检查是否有漏采。
- 每60分钟唤醒一次；程序仅在交易日、收盘后18:30至23:30或发现历史缺口时执行，其他时间记录 `skipped` 后退出。
- 失败可在20:30、22:30等后续唤醒中自动重试；电脑睡眠后唤醒也能补采。
- 入口脚本使用绝对路径和固定Python虚拟环境；日志进入 `logs/`；PID/文件锁保证同一时刻只有一个更新任务。
- API密钥（如后续选用付费数据源）仅放在macOS Keychain或受限环境变量，不进入代码、数据库、plist或日志。

`scripts/deploy_runtime.py` 将运行时部署到 `~/Library/Application Support/HighDividend`，避开macOS对“文稿”目录的后台访问限制；`scripts/install_launchd_agent.py --install` 安装每日任务，`scripts/install_low_frequency_agent.py --install` 安装每周低频任务。运行时数据库才是本机服务的正式数据库。除非明确要重置运行时，不要使用 `--refresh-database`，以免用开发种子库覆盖已发布版本。

## 数据源原则

现有AKShare/Eastmoney/Sina/BaoStock仅适合作为一次性便利源；配置文件已标为“自动化许可未知”。自动更新前必须：

1. 为每个自动数据源确认允许定时抓取、保存和派生展示的条款；或改用明确许可的API。
2. 将来源写入可配置的优先级与允许清单；价格主源失败后才能使用备用源。
3. ETF分配、公司分红和财报保留“采集状态”与“公告核验状态”两个字段；未核验ETF分配不得作为策略收益率。

## 数据库改造

保留现有事实表和历史快照，增加：

- `ingestion_runs`、`ingestion_failures`、`quality_checks`、`ingestion_watermarks`；
- `provider_priority`、`record_approval`、`quarantine_records`、`database_releases`；
- `security_master`、`universe_definitions`、`screening_runs`、`screening_results`；
- 分开维护 `collection_status`、`verification_status`、`last_checked_at`，不再用单一 `data_status` 混合表达。

行情和分红使用自然键、版本和生效时间去重；“当前值”视图按已批准版本、交易日期、来源优先级选取，而不是简单按最近导入运行排序。财务数据另提供PIT（当时已披露）视图。

## 标的池分层

| 层级 | 目标规模 | 用途 |
|---|---:|---|
| 全市场事实库 | 全部可交易A股及场内ETF，动态清单 | 不预先判断是否高股息 |
| 规则初筛池 | 约300–800只 | 依据连续分红、流动性、ST/停牌、数据完整度等透明规则每日/每周生成 |
| 深度研究池 | 40–80只股票、15–25只ETF | 人工核验、分类和长期跟踪 |
| 策略输出池 | 依规则与IPS动态生成 | 不是自动买入清单 |

航运、煤炭、油气、钢铁、化工等周期行业必须单独分桶；银行/保险使用资本和资产质量口径；ETF需另存规模、费率、NAV、折溢价、跟踪偏离、持仓与分配信息。

## 验收标准

- 连续10天（含周末）运行测试；
- 断网、单源失败、电脑睡眠/重启、重复启动和SQLite损坏都不污染正式库；
- 可自动补齐缺失交易日；
- 每次运行均可查看覆盖率、最新交易日、耗时、源、失败清单和发布版本；
- SQLite完整性、外键、日期、价格异常、分红重复和数据陈旧度自动校验；
- 任一历史原始运行可重建对应数据库版本；
- 可一键停止LaunchAgent，或从最近发布备份回滚。

## 推荐实施顺序

1. 确定可自动化的数据源与许可；
2. 增加水位线、质量门禁、发布/回滚和状态表；
3. 完成单次 `--dry-run`、`--once`、失败注入测试；
4. 生成LaunchAgent但先手动加载测试；
5. 建全市场证券主表与规则初筛池；
6. 最后建设策略库与桌面端。
