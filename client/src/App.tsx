import { useEffect, useMemo, useState } from "react";
import { Candidate, core, Coverage, Dashboard, Strategy, StrategyRun } from "./core";

type Page = "home" | "research" | "strategies" | "draft" | "method";

function formatDate(value?: string) {
  if (!value) return "未知";
  return value.replace("T", " ").replace("+00:00", "");
}

function metric(candidate: Candidate, lane?: Strategy["lane"]) {
  if (lane === "cyclical") return "仅观察，不计算收益率";
  if (lane === "etf") return "ETF资料待补齐";
  const payload = candidate.payload;
  const stockYield = payload.ordinary_ttm_cash_yield_pre_tax;
  if (typeof stockYield === "number") return `TTM已实施税前历史 ${(stockYield * 100).toFixed(2)}%`;
  return "历史现金口径待补齐";
}

export default function App() {
  const [page, setPage] = useState<Page>("home");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [activeStrategy, setActiveStrategy] = useState<Strategy | null>(null);
  const [run, setRun] = useState<StrategyRun | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDashboard = async () => {
    setBusy(true);
    setError(null);
    try {
      setDashboard(await core<Dashboard>("dashboard"));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法读取本机发布包");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { void loadDashboard(); }, []);

  const openStrategy = async (strategy: Strategy) => {
    setActiveStrategy(strategy);
    setPage("research");
    setBusy(true);
    setError(null);
    try {
      if (!dashboard) throw new Error("尚未固定当前发布版本，请先刷新首页");
      const result = await core<StrategyRun>("run_strategy", { strategy_id: strategy.strategy_id, release_id: dashboard.release.release_id });
      setRun(result);
      const resultList = await core<{ candidates: Candidate[] }>("candidates", { strategy_run_id: result.strategy_run_id });
      setCandidates(resultList.candidates);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "策略运行失败");
    } finally {
      setBusy(false);
    }
  };

  const visibleCandidates = useMemo(() => candidates.filter((item) => {
    if (item.research_state === "资料不足" || item.research_state === "不纳入本模板") return false;
    if (activeStrategy?.lane === "cyclical") return item.research_state === "继续观察";
    return item.research_state === "资料足以研究";
  }), [activeStrategy, candidates]);

  const saveWatch = async () => {
    if (!selected || !run) return;
    try {
      await core("save_watch", { release_id: run.release_id, instrument_id: selected.instrument_id, note: "待人工复核", review_condition: "下一次年报或分红公告" });
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存观察失败");
    }
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">高</span><div><strong>高股息研究</strong><small>本机 · 研究版</small></div></div>
        <nav>
          <button className={page === "home" ? "nav-item active" : "nav-item"} onClick={() => setPage("home")}>今天看什么</button>
          <button className={page === "strategies" ? "nav-item active" : "nav-item"} onClick={() => setPage("strategies")}>策略库</button>
          <button className={page === "research" ? "nav-item active" : "nav-item"} onClick={() => setPage("research")}>候选与解释</button>
          <button className={page === "draft" ? "nav-item active" : "nav-item"} onClick={() => setPage("draft")}>我的 20 万草案</button>
        </nav>
        <button className="method-link" onClick={() => setPage("method")}>数据与方法 <span>↗</span></button>
        <div className="sidebar-foot">不连接券商 · 不自动交易<br />历史现金分配不是未来承诺</div>
      </aside>
      <main className="main-content">
        <header className="topbar"><span>研究工作台</span><span className="offline-pill"><i />本机离线</span></header>
        {error && <div className="error-banner">{error}<button onClick={() => setError(null)}>×</button></div>}
        {page === "home" && <Home dashboard={dashboard} busy={busy} onRefresh={loadDashboard} onStrategy={openStrategy} onStrategies={() => setPage("strategies")} />}
        {page === "strategies" && <StrategyLibrary strategies={dashboard?.strategies ?? []} onOpen={openStrategy} />}
        {page === "research" && <ResearchPage strategy={activeStrategy} run={run} candidates={visibleCandidates} busy={busy} selected={selected} onSelect={setSelected} onWatch={saveWatch} onBack={() => setPage("strategies")} />}
        {page === "draft" && <DraftPage />}
        {page === "method" && <MethodPage dashboard={dashboard} />}
      </main>
    </div>
  );
}

function Home({ dashboard, busy, onRefresh, onStrategy, onStrategies }: { dashboard: Dashboard | null; busy: boolean; onRefresh: () => void; onStrategy: (strategy: Strategy) => void; onStrategies: () => void }) {
  const stable = dashboard?.strategies.find((item) => item.lane === "stable");
  const stableCount = dashboard?.latest_runs[stable?.strategy_id ?? ""] ? "已运行" : "等待运行";
  return <section className="page home-page">
    <div className="hero"><div><p className="eyebrow">今天看什么</p><h1>先看清楚，<em>再决定。</em></h1><p className="hero-copy">这是你的本地高股息研究台。先从资料完整的企业开始，ETF 和周期标的分别看，不把一个历史收益率当成答案。</p></div><div className="hero-orb">200k<span>研究本金基准</span></div></div>
    {dashboard ? <div className="release-strip"><span className="status-dot" />事实已发布 <strong>{dashboard.release.release_id}</strong><span>发布可用时间 {formatDate(dashboard.release.available_cutoff)}</span><span>{coverageSummary(dashboard.release.coverage)}</span><button onClick={onRefresh}>刷新</button></div> : <div className="release-strip warning"><span className="status-dot" />正在读取本机事实发布包… <button onClick={onRefresh}>重试</button></div>}
    <div className="section-heading"><div><p className="eyebrow">研究入口</p><h2>从一个方向开始</h2></div><button className="text-button" onClick={onStrategies}>查看策略库 →</button></div>
    <div className="strategy-grid">
      {stable && <button className="strategy-card featured" onClick={() => onStrategy(stable)}><div className="card-label">建议从这里开始</div><div className="strategy-icon green">稳</div><h3>{stable.name}</h3><p>{stable.purpose}</p><span className="card-link">{stableCount} · 打开研究清单 →</span></button>}
      {dashboard?.strategies.filter((item) => item.lane !== "stable").map((strategy) => <button className="strategy-card secondary" key={strategy.strategy_id} onClick={() => onStrategy(strategy)}><div className="strategy-icon">{strategy.lane === "etf" ? "指" : "周"}</div><h3>{strategy.name}</h3><p>{strategy.purpose}</p><span className="card-link">换一种研究方式 →</span></button>)}
      {!dashboard && <div className="empty-card">本机发布包尚未可读。<br /><button className="text-button" onClick={onRefresh}>重新检查</button></div>}
    </div>
    <div className="notice-box"><span>i</span><div><strong>研究清单不是买入清单</strong><p>“资料足以研究”只表示数据完整并满足规则，尚未做估值判断。所有价格都是已发布收盘价，所有年现金都是税前历史参考。</p></div></div>
  </section>;
}

function StrategyLibrary({ strategies, onOpen }: { strategies: Strategy[]; onOpen: (strategy: Strategy) => void }) {
  return <section className="page"><p className="eyebrow">策略库</p><h1>三种研究方式</h1><p className="lead">策略是研究框架，不是自动买卖信号。每次运行都会固定发布版本和规则版本。</p><div className="library-list">{strategies.map((strategy) => <div className={`library-row ${strategy.lane}`} key={strategy.strategy_id}><div className="lane-tag">{strategy.lane === "stable" ? "稳健" : strategy.lane === "etf" ? "ETF" : "周期"}</div><div className="library-copy"><h3>{strategy.name}</h3><p>{strategy.purpose}</p><small>适合：{strategy.suitable_for}</small><small>不包含：{strategy.excludes}</small></div><button className="outline-button" onClick={() => onOpen(strategy)}>打开</button></div>)}</div></section>;
}

function ResearchPage({ strategy, run, candidates, busy, selected, onSelect, onWatch, onBack }: { strategy: Strategy | null; run: StrategyRun | null; candidates: Candidate[]; busy: boolean; selected: Candidate | null; onSelect: (candidate: Candidate | null) => void; onWatch: () => void; onBack: () => void }) {
  const emptyLabel = strategy?.lane === "cyclical" ? "当前没有可观察的周期标的。" : "当前发布包暂无资料足以研究的标的。";
  return <section className="page research-page"><button className="back-link" onClick={onBack}>← 策略库</button><div className="page-heading"><div><p className="eyebrow">候选与解释</p><h1>{strategy?.name ?? "选择一个研究方式"}</h1><p className="lead">符合研究条件，不等于买入建议。列表按资料状态排序，不按收益率推荐。</p></div>{run && <div className="run-badge">运行 {run.strategy_run_id}<small>{Object.entries(run.screen.states).map(([key, value]) => `${key} ${value}`).join(" · ")}</small></div>}</div>{busy && <div className="loading">正在读取固定发布版本…</div>}{!busy && candidates.length === 0 && <div className="empty-state">{emptyLabel}<br /><small>请查看数据覆盖，或等待低频事实补齐。</small></div>}<div className="candidate-layout"><div className="candidate-list">{candidates.map((candidate) => <button className={`candidate-row ${candidate.research_state === "资料足以研究" ? "qualified" : "watch"}`} key={candidate.instrument_id} onClick={() => onSelect(candidate)}><div className="candidate-main"><strong>{String(candidate.payload.name ?? candidate.instrument_id)}</strong><small>{String(candidate.payload.ticker ?? "")} · {candidate.research_state}</small></div><div className="candidate-metric"><strong>{metric(candidate, strategy?.lane)}</strong><small>{candidate.payload.price_date ? `收盘价 ${candidate.payload.price_date}` : "价格未知"}</small></div><span className="arrow">→</span></button>)}</div>{selected && <aside className="research-drawer"><button className="drawer-close" onClick={() => onSelect(null)}>×</button><p className="eyebrow">研究卡</p><h2>{String(selected.payload.name ?? selected.instrument_id)}</h2><span className={`state-pill ${selected.research_state === "资料足以研究" ? "green-pill" : "yellow-pill"}`}>{selected.research_state}</span><div className="fact-block"><label>历史现金口径</label><p>{String(selected.payload.boundary ?? "已实施现金分配历史参考")}</p></div><div className="fact-block"><label>为什么在这里</label><p>{selected.reason_codes.join("、")}</p></div><div className="fact-block"><label>还需要回答</label><p>尚未进行估值判断；请在下一次年报或分红公告后复核。</p></div><button className="primary-button" onClick={onWatch}>加入我的观察</button></aside>}</div></section>;
}

function coverageSummary(coverage: Coverage[]) {
  const prices = coverage.filter((item) => item.dataset === "market_prices").reduce((sum, item) => sum + item.collected, 0);
  const stocks = coverage.find((item) => item.dataset === "stock_dividends");
  const etfFacts = coverage.find((item) => item.dataset === "etf_product_facts");
  return `价格 ${prices.toLocaleString()}/${coverage.filter((item) => item.dataset === "market_prices").reduce((sum, item) => sum + item.instruments, 0).toLocaleString()} · 股票分红 ${stocks?.collected ?? 0}/${stocks?.instruments ?? 0} · ETF产品 ${etfFacts?.collected ?? 0}/${etfFacts?.instruments ?? 0}`;
}

function DraftPage() {
  return <section className="page"><p className="eyebrow">我的 20 万草案</p><h1>配置与风险边界</h1><p className="lead">金额草案与导出门禁正在接入策略运行结果。先把四个边界想清楚，系统才会开放金额计算。</p><div className="boundary-grid"><div><label>投资期限</label><div className="select-placeholder">尚未填写 <span>⌄</span></div></div><div><label>未来现金需求</label><div className="select-placeholder">尚未填写 <span>⌄</span></div></div><div><label>风险档</label><div className="risk-options"><button>稳健</button><button>均衡</button><button>进取</button></div></div><div><label>是否允许周期研究</label><div className="risk-options"><button>允许观察</button><button>不允许</button></div></div></div><div className="locked-box"><span>🔒</span><div><strong>金额与数量尚未开放</strong><p>导出前仍需确认发行人、行业、周期和 ETF 上限，并录入券商当日价格重算。当前不会生成买入指令。</p></div></div></section>;
}

function MethodPage({ dashboard }: { dashboard: Dashboard | null }) {
  return <section className="page"><p className="eyebrow">数据与方法</p><h1>每个结论都有出处</h1><p className="lead">这里显示发布版本、口径和覆盖状态；采集日志与规则参数保持在本地，不干扰日常研究。</p><div className="method-card"><div><label>当前发布</label><strong>{dashboard?.release.release_id ?? "未读取"}</strong></div><div><label>事实哈希</label><code>{dashboard?.release.facts_sha256?.slice(0, 20) ?? "—"}…</code></div><div><label>价格口径</label><strong>已发布收盘价（日期）</strong></div><div><label>现金口径</label><strong>过去 12 个月已实施、税前历史参考</strong></div></div><h2 className="subheading">数据覆盖</h2><div className="coverage-list">{dashboard?.release.coverage.map((item) => <div key={item.dataset}><span>{item.dataset}</span><strong>{item.collected} / {item.instruments}</strong><small>{(item.average_completeness * 100).toFixed(1)}% 平均完整度</small></div>)}</div></section>;
}
