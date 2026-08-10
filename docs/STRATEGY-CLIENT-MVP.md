# 策略库与客户端 MVP

## 已交付

策略库现在包含三个版本化研究框架：

- `stable_dividend_stock_research@1.0.0`：常规分红股票研究；
- `dividend_etf_research@1.0.0`：权益红利 ETF 分散研究；
- `cyclical_dividend_satellite_watch@1.0.0`：周期红利卫星观察。

每次运行都会写入 `workbench.db`：`release_id`、事实哈希、策略版本、筛选运行编号和结果状态；同时记录策略、规则和 taxonomy 的内容哈希与原文快照到 `immutable_artifacts_v1`。历史运行不会被“当前”事实覆盖。

## 客户端三步

Tauri + React 客户端只有三个主入口：

1. **今天看什么**：显示当前发布、覆盖情况和三个研究方式，默认高亮稳健分红；
2. **候选与解释**：显示非推荐排序的候选，打开研究卡、查看原因并加入观察；
3. **我的 20 万草案**：显示配置与风险边界。金额、数量和人工核对清单在下一里程碑接入完整导出门禁前保持锁定。

客户端只调用本地 Python 内核的 JSON-stdio 命令，不打开 HTTP 端口，不让前端接触 SQL。所有可保存的策略运行和观察记录都绑定发布版本。

## 当前实际数据边界

使用当前发布包 `release_20260810_v3` 时：

- 股票：22 只“资料足以研究”、9 只周期标的不纳入稳健模板，其余资料不足；
- ETF：由于产品事实尚未接入，全部保持“资料不足”；
- 周期观察：9 只“继续观察”，不参与稳健股票研究。

这些状态反映数据覆盖和规则结果，不是买入建议或收益预测。

## 本地运行

安装包已内置 scripts、rules、strategies、config 和 database 资源。首次启动会先做 Runtime 预检；缺少代码时自动复制到 `~/Library/Application Support/HighDividend`，不会覆盖 `data/`、发布包或 workbench 数据。若本机没有事实发布包，客户端会明确提示数据运行时未就绪。

手动部署/修复运行时仍可使用：

```bash
python3 scripts/deploy_runtime.py
python3 scripts/strategy_engine.py --list
```

启动前端开发服务器：

```bash
cd client
npm install
npm run dev
```

构建桌面壳：

```bash
npm run tauri -- dev
```

客户端不会自行抓取网络；数据更新仍由本机发布任务负责。
