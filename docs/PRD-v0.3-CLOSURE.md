# PRD v0.3 阻断项关闭记录

- 基线：`docs/PRD-v0.2-REVIEW.md`
- 关闭版本：`docs/PRD.md` Draft v0.3 — Data Collection Baseline
- 日期：2026-08-09
- 结论：v0.2列出的10项Spike执行契约阻断均已在文档或机器可读附件中关闭；批准进行受限的本地数据可行性Spike。此结论不批准投资建议或正式MVP。

| v0.2阻断项 | v0.3关闭方式 | 验证证据 |
|---|---|---|
| 冻结样本与fixtures | 固定6个instrument_id、区间、as-of；公司行动和陷阱使用独立合成夹具 | `config/spike_universe.json`、`tests/fixtures/golden_fixtures.json` |
| 投资决策契约不确定 | 冻结本金、假设集、行业DPS约束、四态映射；IPS不完整时组合状态只能为未评估 | `docs/PRD.md`的IPS、AssumptionSet与状态章节 |
| 高息陷阱结果不唯一 | `SYN-TRAP-001`唯一预期为排除，同时返回两个原因码且禁止隐含价格 | `tests/fixtures/golden_fixtures.json` |
| 分红事件身份与选版 | 固定逻辑键、event/version/supersedes字段、TTM选取与冲突规则 | `docs/PRD.md`的领域模型与双时间章节 |
| 双时间语义 | 分离市场当时可知视图与系统当时持有视图，定义左闭右开版本区间 | `docs/PRD.md`的双时间查询契约 |
| 总回报递推不唯一 | 固定未复权价格、除息日收益、支付日现金账、再投资与1bp含义 | `config/formula_registry.json`、`SYN-CA-001` |
| 税务期望不精确 | 固定FIFO、持有期税率、递延扣税与个股/ETF精确税额 | `SYN-TAX-A-001`、`SYN-TAX-ETF-001` |
| 来源与许可矩阵缺失 | 建立六类权利矩阵、一级来源登记及一次性本地调查边界 | `config/source_matrix.csv`、`config/primary_source_registry.json` |
| 输出、原因码与hash未冻结 | 固定输出必填字段、原因码、SHA-256规范化和运行时字段排除 | `config/reason_codes.json`、`docs/PRD.md` |
| 组合穿透优先级冲突 | 最小发行人/行业超限为MUST，完整穿透仍为SHOULD | `SYN-IPS-001`及PRD测试12 |

## 桌面执行复核

- 6个golden fixtures全部通过确定性校验。
- 同一输入构建`run_20260809_composite_v2a`与`run_20260809_composite_v2b`，两次规范化内容hash完全相同：`aab54bfac4a09f988098dc467843fc870c5a91d5c0498b82e32ec6e7884b80b1`。
- 该复核只证明已实现契约的确定性及首批数据覆盖，不证明完整Spike退出门槛已经通过。
