# 规则初筛池、策略库与桌面端设计 v2

- 状态：修订设计，尚未开发
- 用户：单人、本机、20 万元研究本金、长期高股息研究、人工下单
- 目标：把已发布事实缩小为可复核的研究清单；不提供实时交易或自动买卖
- 核心原则：买的是企业或指数暴露，不是一个历史股息率数字

## 1. 产品边界与当前覆盖

当前正式库有全市场证券主表和日终行情（5,422 只股票、1,603 只 ETF），但低频分红、ETF 分配和财务事实完整覆盖的仍是 31 只核心股票和 5 只核心 ETF。这个差异是产品状态，不是要在界面中隐藏的细节。

因此，第一版不产生“全市场高股息排行榜”。它只展示：

- **资料足以研究**：满足某一模板的全部数据合同；
- **继续观察**：有研究价值，但周期、产品机制或特殊分红需要人工复核；
- **不纳入本模板**：触发了模板的明确排除条件；
- **资料不足**：缺低频事实、公告核验、行业规则、流动性窗口或产品资料，不排序、不生成金额草案。

前台固定提示：**“进入研究清单只表示资料完整并符合研究规则；不代表估值合理、未来分红可持续或适合买入。”**

## 2. 不可变发布包，而不是持续写入的活库

采集程序可以维护活的暂存事实库，但筛选、桌面端、策略、草案和导出只读取不可变发布包。

```text
runtime/staging.db                         # 采集任务可写，用户界面不可读
runtime/releases/<release_id>/
  facts.sqlite                             # 完整、只读事实快照
  facts.sqlite.sha256                      # 事实快照内容哈希
  manifest.json                            # 含 facts_sha256、大小、as_of、各数据集水位线、覆盖率、QC、schema
  manifest.sha256
  raw-inputs.json                          # 原始采集run及hash引用
  READY                                    # 完整发布标记
runtime/current-release.json               # 原子切换的当前发布指针
```

每日行情和每周低频任务只写暂存区。唯一发布器取得 `publisher.lock` 后，在 `release.tmp` 构建通过质量门禁的 `facts.sqlite`，先执行 SQLite `integrity_check`，计算事实文件哈希和大小，写入 manifest/各自哈希并校验全套文件；随后原子重命名为 `releases/<release_id>`，最后才原子更新 `current-release.json`。启动桌面端、恢复、打开旧草案都必须验证 `facts.sqlite` 哈希与 manifest 一致且存在 `READY`，否则拒绝读取该发布包。同一发布包必须逐项记录：

`market_prices / stock_dividends / ETF_distributions / financials / ETF_product_facts` 的 `as_of_date、available_cutoff、watermark、coverage、quality_status、source_run_ids`。

`latest` 仅允许首页临时浏览。保存筛选、策略运行、草案和导出时，必须绑定显式 `release_id`。

发布包执行可审计保留策略：保留最近 `N_daily` 个日版本、最近 `N_lowfreq` 个低频/月度版本，以及所有被锁定草案、导出记录引用的版本。`N_daily`、`N_lowfreq`、引用版本最长保留期、警戒和硬停止磁盘水位均是版本控制的运行时配置。磁盘达到警戒水位时暂停非必要发布并提示清理；清理不得删除被引用版本，且每次清理写入审计记录。

## 3. 两库边界与统一主数据

第一版收敛为两库，避免研究、个人资料和事实跨库引用：

| 存储 | 写入者 | 内容 | 约束 |
|---|---|---|---|
| 发布包 `facts.sqlite` | 发布器 | 主数据、行情、分红、ETF 分配、财务、来源、质量 | 严格只读 |
| `workbench.db` | 桌面端/规则引擎 | 规则、策略、筛选结果、IPS、观察、笔记、草案、决策日志 | 不得修改事实 |

`facts.sqlite` 只保留事实、采集运行、质量门禁和发布元数据。现有 `universe_definitions`、`screening_runs`、`screening_results` 被标为兼容表，新的筛选运行一律写入 `workbench.db`。

`instrument_id` 是唯一稳定主键。发布器必须维护 `instruments ↔ security_master` 的不可变映射；规则或桌面端不得以代码字符串临时拼接两套主表。

## 4. 覆盖矩阵与 PIT 字段合同

每个标的、每个数据域均有机器可判定的覆盖记录：

```text
CoverageMatrix =
instrument_id + dataset + required_history_window
+ latest_effective_date + latest_available_date
+ collection_status + verification_status
+ completeness_ratio + freshness_status + source_tier
+ release_id
```

每个规则模板声明 `DataRequirements`，例如“稳健分红股票”要求已实施分红历史、行业适用的财务数据、流动性窗口、行业标签和可用价格都达到指定窗口；ETF 模板要求指数、产品分类、分配/净值、规模、费率、成交和持仓披露分别满足要求。

任何可用于筛选的事实字段都遵循 PIT 合同：

```text
report_period_end
published_at
effective_at
observed_at
source_version
verification_status
```

筛选只能使用 `published_at <= release.available_cutoff` 的信息。缺公告时间、修订链、实施状态或来源核验时，字段不可用于“资料足以研究”。上市未满观察期、停牌、退市和更名都有显式生命周期状态，不得因历史缺失而静默消失。新上市但尚无完整观察期的标的显示为“观察期未满，尚不能进入稳健模板”，而非“公司质量差”或永久资料不足。

## 5. 收益率与分配口径

### 股票

```text
ttm_cash_dividend_yield_gross =
过去12个月、在cutoff前已实施且公告已核验的税前每股现金股利（公司行动调整后）
÷ 该release对应的已发布收盘价
```

必须同时保存并展示：常规分红、特别分红、事件状态、除权日、价格日期、税前/税后口径和公司行动调整标志。特别分红可作为历史现金事实显示，但默认不计入“稳健分红”核心筛选。

### ETF

ETF 只使用 **“过去 12 个月现金分配率（非股息率、非总回报）”**。它是基金实际已实施分配 ÷ 已发布收盘价，必须同屏显示“该数字不代表指数股息率、基金未来分配或基金总回报”。股票和 ETF 永不跨池排序。

ETF 分配也须以事件合同复现：使用实施的每份现金分配，在拆分、合并、现金替代发生后统一调整份额；保存权益登记/除权基准、公告与实施日期、复权/公司行动因子及价格基准日期。缺任一关键事件信息，不显示完整的 12 个月现金分配率。

### 周期标的

航运、煤炭、油气、钢铁、化工等周期行业永不进入“稳健分红”模板。周期研究卡并列展示：长周期利润/现金分红区间、当前利润相对历史中位数/峰值、一次性因素、行业景气代理的日期、`normalised_dividend_capacity`。后者以 RuleVersion 明确观察期、常规/特别分红处理、行业对应的利润或现金流口径、允许缺失和展示标签；它只用于风险复核，不是未来分红预测。缺此合同或关键事实时只能“继续观察”。

## 6. 行业与 ETF 产品规则集

规则按受控词表 `sector_rule_set` 生效；无对应规则集即资料不足。行业或周期人工覆盖必须以追加记录保存 `author、reason、effective_from、review_due、previous_value`，不能直接改写历史标签：

- 普通工商：利润、经营现金、自由现金、净负债、派息记录；
- 银行：资本充足、资产质量/拨备、盈利、派息与监管约束；不使用经营现金流覆盖；
- 保险：偿付能力、投资收益波动、资本约束、派息记录；
- 公用事业：经营现金、维持/扩张资本开支、债务期限、监管回报或电价机制；
- 周期行业：正常化能力和景气风险优先于 TTM 收益率。

第一版 ETF 范围仅限：**境内交易所上市、权益红利指数 ETF、产品资料完整**。债券、商品、货币、QDII、REITs 及产品类型未知基金都单列或资料不足。ETF 研究必须有指数方法、基金分配机制、费用、规模、成交、跟踪偏离/折溢价、清盘风险、管理人变化、前十大和行业集中度；数据缺失不得给绿色完整性状态。

## 7. 可运行的规则与策略模型

规则是版本化声明，不要求用户写 SQL。每个 `RuleVersion` 固化：字段目录版本、参数 schema、单位、`all/any/not` 条件树、比较器、缺失策略、稳定并列排序、原因码、引擎版本、生效/弃用日期。模板参数不完整即不可运行，不能用实现者默认值代替。

```json
{
  "id": "stable_dividend_stock",
  "version": "1.0",
  "scope": "a_share_common_stock",
  "data_requirements": ["verified_dividends_3y", "sector_financial_contract", "liquidity_20d", "fresh_eod_price"],
  "required_parameters": ["min_ordinary_dividend_years", "dividend_interruption_definition", "min_ordinary_ttm_yield_or_relative_benchmark", "sector_coverage_or_capital_status"],
  "eligibility": {"all": [
    {"field": "data_readiness", "op": "=", "value": "complete"},
    {"field": "ordinary_dividend_history", "op": "meets", "value": "min_ordinary_dividend_years"},
    {"field": "ordinary_ttm_cash_dividend_yield_gross", "op": "meets", "value": "min_ordinary_ttm_yield_or_relative_benchmark"},
    {"field": "sector_coverage_or_capital_status", "op": "=", "value": "pass"}
  ]},
  "missing_data_policy": "data_insufficient",
  "ranking": {"primary": "research_priority", "tie_breaker": "instrument_id"},
  "reasons": {"data_insufficient": "关键分红、财务或流动性资料不足"}
}
```

“常规分红”排除特别/一次性分红；“中断”由该版本明确（例如观察年度内未实施常规现金分红），不得由界面临时解释。`research_priority` 仅是研究顺序字段：其构成、计算版本和原因码必须写进字段目录与 RuleVersion，且界面不得把它显示为投资评分或推荐。三张模板都须声明各自的历史窗口、核心收益率/分配率口径、行业或产品约束及入口区间或相对基准；数据合同与策略门槛同时通过，才能进入对应研究池。

ETF 模板也须有同等具体的 RuleVersion 示例：范围为权益红利指数 ETF，必填参数包括产品资料完整、实施分配/净值事件完整、最低流动性窗口和跟踪品质状态；任何参数或事件缺失均按 `data_insufficient` 处理。

前台不用 `eligible/watch/blocked`，而使用“资料足以研究 / 继续观察 / 不纳入本模板 / 资料不足”。首版不把估值写入入池或排除逻辑，而明确显示“尚未做估值判断”。

策略 = 标的范围 + 规则模板 + 研究排序 + 组合约束 + 复核规则 + 失效条件。首版只有三张研究策略卡：

1. 稳健分红股票研究；
2. 红利 ETF 分散研究；
3. 周期红利卫星观察。

“现金流再平衡”是组合草案内的检查功能，不单列为策略。策略输出是研究清单与理由，不是买入或卖出指令。

## 8. 配置与风险边界、组合草案与导出安全门禁

内部数据模型称 IPS；前台统一称“配置与风险边界”，分两层：

- 浏览/观察只需轻量资料：研究本金（预填 200,000 元）、偏好（稳健/均衡/ETF 优先）、是否允许周期研究；
- 生成金额、数量或导出清单前，前台以“配置与风险边界”收集四项简短信息：期限、现金需求、风险档、是否允许周期；风险档生成可见且可一键修改的单标的/行业/周期/ETF 上限与现金预留。风险档至少为“稳健 / 均衡 / 进取”，并在当页展示各自边界；“是否依赖分红现金流、可接受永久损失”是两项确认提示，不是新增问卷字段。用户须确认所生成的边界。

默认上限只能是“待确认”，不得暗中预填比例。缺任何必填项、金额超过本金、约束超限、数据不足或价格过期时，草案只能保存为研究记录，不得导出。

草案状态机：`draft → reviewed → locked → archived`。锁定草案保存 `release_id、rule_version、strategy_version、IPS snapshot、reference_price、price_as_of、lot_size、舍入规则、费用/税费假设、现金规则、全部约束结果`。

桌面端没有实时价格时，所有价格均称 **“已发布收盘价（日期）/参考价”**，绝不称“当前价”。参考价超过一个交易日，或用户录入的券商价格相对参考价偏离超过 2%，草案标记“需重算”。导出时必须使用用户确认的当日价格重新按交易单位取整，并标注：

> 非订单；仅为税前历史现金分配参考，未计算税费；按过去 12 个月实际已实施现金分配的静态估算，非未来承诺；以券商当日价格、最小交易单位和可成交状态为准。

ETF 与直接持股须检查行业、前十大和可得时的单一成分股重叠。组合金额、比例、数量、交易费用和预留现金必须一致，否则不能导出。

## 9. 极简桌面端

前台只有三步；技术名、日志、规则 JSON 和哈希只在“数据与方法”抽屉中可查。

### 第一步：今天看什么

- 显示最后成功发布、下次合资格检查窗口、价格/分红/财务分别截至哪天；
- 覆盖提示必须明确，例如“股票低频事实：31 / 5,422；ETF：5 / 1,603”；
- 选择研究方式：稳健分红、红利 ETF 或周期观察；
- 无健康发布包时只显示数据说明；价格超过新鲜度阈值时可研究、不可导出草案。

### 第二步：候选与解释

默认只展示几十个资料足以研究或继续观察的对象，不展示七千个证券。首屏按“资料完整 → 最近复核 → 代码”稳定排序，并明确标注“非买入排序”；无结果时显示“当前发布包暂无资料足以研究的标的”、覆盖缺口及下次数据补齐动作。每行：

`名称｜已发布收盘价（日期）｜股票TTM已实施现金收益率 或 ETF过去12个月现金分配率｜状态｜一句风险｜资料为什么足以研究`

可加入“我的观察”、保存等待条件或比较 2–4 个标的。首次用户默认高亮“稳健分红股票研究”；每张卡只显示“适合 / 不包含 / 注意”，ETF 与周期放在“换一种研究方式”内，周期观察不与稳健入口同等强调。等待条件提供“下次年报”“下一次分红公告”“数据补齐”“价格重新确认”等预设模板。研究卡固定写“符合研究条件，不等于买入建议”，并显示尚未回答的问题、数据来源、更新日期和行业/产品风险。历史现金分配与历史总回报分屏展示，永不相加或互相代替。

### 第三步：我的 20 万草案

只在“配置与风险边界”和数据门槛通过后开放金额、数量和导出；否则展示缺哪些资料。用户可以保存笔记、人工例外与复核结论。超限例外可以保存，但必须写 `override_reason`，且永远不能绕过导出门禁。

## 10. 内部接口、技术边界与恢复

桌面端不拼 SQL，也不访问网络。接口显式区分当前研究与历史快照：

```text
openCurrentDashboard()
openRelease(releaseId)
runScreen({releaseId, ruleVersionId, parameters})
openCurrentResearchCard(instrumentId)
openSnapshotResearchCard({instrumentId, screenRunId})
createPortfolioDraft({strategyRunId, ipsSnapshotId})
recalculateDraft({draftId, confirmedBrokerPrices})
validateDraft(draftId)
exportManualChecklist(lockedDraftId)
```

macOS 壳建议使用 Tauri + React/TypeScript。Tauri 用 Rust SQLite 驱动只读发布包；规则和组合计算使用受控 Python sidecar，经版本化 JSON stdin/stdout 通信，不开启本地 TCP 端口。桌面端、sidecar 和 `launchd` 共享带 `schema_semver` 的运行时包；manifest 还记录 App 壳、Rust 查询层、Python sidecar、发布器版本。兼容矩阵明确 `facts_schema`、`manifest_schema`、`workbench_schema`、sidecar API 的最小可读/可运行版本。启动时按此检查：新 App 可只读降级读取旧包；旧 App 遇到新 schema 只显示“需要升级”；任何组合均不自动迁移事实库。

发布包本身是事实历史备份。`workbench.db` 每日使用 SQLite online backup，并在 IPS 变更、草案锁定和导出后创建版本化备份；恢复流程必须是“导入临时副本 → integrity_check / manifest / schema 校验 → 显示差异 → 原子切换或另存副本”，恢复前的当前库必须保留为副本，不得直接覆盖。规则、草案、笔记和决策清单可各自导出为带 hash 的 JSON；离线诊断包只含版本、覆盖率、错误类型和 hash，不含个人笔记或完整行情。原始数据源只按其许可展示派生结论；许可未知的原始数据不可批量导出。

## 11. 修订后的实施顺序与验收

1. 统一主数据、不可变发布包、全局发布器、发布锁与覆盖矩阵；
2. PIT 字段目录、分红/ETF口径、行业规则集和 ETF 产品分类；
3. 规则 DSL、理由输出、低频数据补齐队列；
4. `workbench.db`、策略版本、IPS、草案状态机和备份恢复；
5. Tauri 三步桌面端；
6. 发布包、旧草案、数据库损坏和工作库恢复演练后验收。

验收必须证明：

- 后来披露的财报或分红不会改变历史发布日的筛选结果；
- 低频覆盖不足时不形成全市场排名；
- 股票股息、ETF现金分配、特别分红和周期能力不混用；
- 旧草案可在原 `release_id` 下重放；
- 价格过期、配置与风险边界缺失、超限或资料不足时绝不能导出清单；
- 任何清单都明确为人工复核材料，非自动交易或投资建议。
