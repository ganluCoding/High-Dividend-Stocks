# High Dividend Stocks

一个本机优先、可审计的中国 A 股与红利 ETF 研究工具。它把免费数据采集得到的可变本地数据库发布为不可变事实快照，再用版本化规则生成**研究清单**；它不是交易系统，也不提供买卖建议。

## 当前能力

- 从已有本地 SQLite 采集库发布带 SHA-256、manifest 与 `READY` 标记的不可变 `facts.sqlite`；
- 用原子 `current-release.json` 指针选择当前版本；
- 为价格、股票分红、财务、ETF 分配和 ETF 产品资料生成覆盖矩阵；
- 将规则、筛选结果写入独立 `workbench.db`，并绑定发布版本和规则版本；
- 提供稳健分红股票与红利 ETF 两个非推荐研究模板；
- 提供稳健股票、红利 ETF、周期观察三个版本化策略框架，以及 Tauri + React 本地客户端 MVP；
- 桌面安装包内置研究 Runtime 资源，启动时会预检并在缺少代码时自举到用户目录；
- 运行记录保存策略、规则和 taxonomy 内容哈希，支持检查历史工件是否被替换；
- 内置离线合成测试，不需要联网或提交真实数据。

当前本地运行时可以包含全市场证券主表与日终价格，但低频事实覆盖会单独显示。项目故意不生成“全市场高股息排行榜”。

## 快速开始

依赖采集器时请安装：

```bash
python3 -m pip install -r requirements.txt
```

将现有本地数据库发布为事实快照：

```bash
python3 scripts/publish_release.py \
  --database "$HOME/Library/Application Support/HighDividend/data/database/high_dividend.db" \
  --runtime-root "$HOME/Library/Application Support/HighDividend"
```

运行股票研究模板：

```bash
python3 scripts/run_screen.py \
  --runtime-root "$HOME/Library/Application Support/HighDividend" \
  --rule rules/stable_dividend_stock_v1.json
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/validate_golden_fixtures.py
```

客户端和策略库说明见 [MVP 文档](docs/STRATEGY-CLIENT-MVP.md)。

启动桌面客户端（macOS）：

```bash
python3 scripts/launch_desktop_client.py --install
```

该命令会先检查本机 Runtime、当前事实发布包和桌面内核，再复制到 `~/Applications/高股息研究.app` 并打开。

## 结果边界

- 股票显示的是过去 12 个月**已实施、税前、常规**现金分红的历史参考；特别分红单列。
- ETF 显示的是过去 12 个月**实际现金分配率**，不是股息率、未来分配或总回报。
- 周期性行业不进入稳健分红模板。
- “资料足以研究”只表示数据与规则通过，不代表估值合理、未来分红可持续或适合买入。

详见 [PRD](docs/PRD-v1.0.md)、[架构设计](docs/NEXT-PHASE-DESIGN.md) 和 [复审报告](docs/NEXT-PHASE-DESIGN-REVIEW-v2.md)。

## 隐私与公开范围

这是公开项目。仓库不提交本地数据库、发布事实、原始抓取数据、日志、个人笔记或个人配置。请在自己的机器上运行采集和发布任务，并遵守各数据源的使用条款。
