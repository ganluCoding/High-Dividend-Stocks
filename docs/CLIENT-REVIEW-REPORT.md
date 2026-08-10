# 桌面客户端三方审阅报告

审阅日期：2026-08-10  
审阅对象：当前分支 Tauri + React 客户端、`desktop_core.py`、`strategy_engine.py` 与运行时发布协议

本报告由三位独立子代理完成：

1. 资深投资顾问 / 产品体验审阅（`common_case_design`）；
2. coding 专家 / Tauri 与本地 IPC 架构审阅（`flexible_design`）；
3. 数据准确性与可解释性审阅（`minimal_design`）。

## 总体结论

客户端的“本地只读、无交易入口、历史分红不是未来承诺、草案暂锁定”边界是正确的。经过本轮修复，分红重复计数、候选通道过滤和策略运行 release 绑定已通过复验；桌面安装包 Runtime 自举、完整 PIT 回放和更丰富的覆盖解释仍未完成。

## 本轮修复复验

- `release_20260810_v2` 已按逻辑事件版本去重，股票收益率重新计算；
- 稳健/ETF/周期三套策略均重跑并绑定该 release；
- React 前端只展示当前策略允许的状态：稳健/ETF 为 `资料足以研究`，周期为 `继续观察`；
- `desktop_core run_strategy` 缺少 `release_id` 时拒绝执行；
- 4 个离线测试、6 个 golden fixtures、TypeScript、Vite 和 Cargo check 均通过。

## P0：必须先处理

### 1. 被规则排除的标的仍显示为候选（已修复）

`client/src/App.tsx:59` 只过滤 `资料不足`，`不纳入本模板` 仍进入 `visibleCandidates`；`App.tsx:118` 又把所有非通过项统一显示成黄色 watch。当前稳定模板实际有 9 只周期股被排除，周期模板也会产生非周期标的，入口分流因此失效。

修复：稳定股票/ETF 只展示 `资料足以研究`；周期入口只展示 `继续观察`；`资料不足` 与 `不纳入本模板` 只保留在运行摘要。

### 2. 股票 TTM 现金收益率重复累加（已修复）

`scripts/run_screen.py:49-69` 对物理分红行求和，当前发布包同一逻辑事件跨 run 重复。美的、长江电力和招商银行的复核值已记录在 [数据准确性报告](./DATA-ACCURACY-REPORT.md)。

修复：发布阶段按逻辑事件版本去重，筛选器再按当前有效事件和 available cutoff 汇总；已重发 release 并重跑策略。

### 3. 可安装 App 没有随包提供研究内核

`client/src-tauri/src/lib.rs:4-26` 固定依赖本机 `/usr/bin/python3` 和 App Support 下的脚本；`client/src-tauri/tauri.conf.json:25-32` 没有 sidecar/resources；`deploy_runtime.py` 不会随 DMG 自动执行。新机器没有预装 runtime 时，App 首次点击即失败。

建议：将 Python 研究内核作为签名 sidecar/resources 打包并首次启动原子部署，或明确提供独立 Runtime 安装器、版本校验和修复入口。

### 4. 策略运行未固定首页刚展示的 release（已修复）

`App.tsx:42-50` 调用 `run_strategy` 时不传 `release_id`，后端缺省读取执行时最新指针。若后台刚发布新 release，用户会看到 A、实际运行 B。

修复：Dashboard 的 release_id 已进入页面状态并传入运行请求；后端缺少 release_id 时拒绝运行。

### 5. 版本工件与 PIT 回放合同不足

策略/规则/taxonomy 从可变 runtime 文件读取；`strategy_versions_v1` 使用 `INSERT OR REPLACE`，运行记录没有完整保存 taxonomy、参数和引擎哈希。筛选查询也没有严格按 `available_cutoff` 过滤披露时间。

建议：以内容哈希保存不可变工件；运行记录保存所有哈希与参数；所有事实通过 PIT 查询层；补历史 cutoff 注入未来披露的回归测试。

## P1：MVP 前应处理

- 主流程没有展示低频覆盖率；当前股票分红/财务仅 31/5,422，ETF 产品事实 0/1,603，容易把小样本误解为全市场结论；
- 股票百分比没有行内标明 TTM、已实施、税前、历史；ETF 没有实际分配率、指数、费用和跟踪风险字段；
- `资料足以研究` 实际只检查覆盖、历史年数和收益率，没有行业财务阈值、现金流/负债反证或 freshness 门槛；
- 周期策略的 `继续观察` 只由 coverage 触发，没有正常化利润、景气代理或特别分红拆分；
- sidecar 无超时/取消/写入队列，Python 非零退出仍可能被当成成功字符串；历史 screen result 未重新验证 release 哈希；
- 首页将 `available_cutoff` 直接写成“数据截至”，但它是发布可用时间，不等于市场价格、财务报告和分红核验的同一日期。

## P2：可用性与维护性

- 草案页的策略/观察按钮当前没有保存逻辑，应禁用并标注“下一里程碑”；
- 加入观察后缺少成功反馈和可编辑复核条件；
- 生产 CSP 不应保留 `http://localhost:1420`；
- `core.ts` 应以 `result === undefined` 判断缺失，不能用 `!result`；
- facts SQLite 读取可使用 `immutable=1`，内部异常应映射为稳定错误码。

## 已验证的正向项

- `npm run check`、`npm run build`、`cargo check` 均通过；
- 3 个离线契约测试和 6 个 golden fixtures 通过；
- 当前客户端没有金额、数量、下单或导出交易指令；
- 页面已明确“历史现金分配不是未来承诺”，草案导出仍被锁定。

## 审阅后的建议顺序

下一阶段补 Runtime bootstrap、不可变策略/规则工件、完整 PIT 回放和覆盖率主路径展示，再继续开发组合策略与桌面端高级能力。
