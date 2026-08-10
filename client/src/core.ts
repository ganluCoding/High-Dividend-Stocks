import { invoke } from "@tauri-apps/api/core";

export type Strategy = {
  strategy_id: string;
  strategy_version: string;
  name: string;
  lane: "stable" | "etf" | "cyclical";
  purpose: string;
  suitable_for: string;
  excludes: string;
  review_triggers: string[];
};

export type Dashboard = {
  release: { release_id: string; available_cutoff: string; facts_sha256: string; coverage: Coverage[] };
  live_collection: { observed_at: string; historical: LiveBatch; low_frequency: { status: string } };
  strategies: Strategy[];
  latest_runs: Record<string, { screen_run_id: string }>;
};

export type RuntimeStatus = {
  runtime_root: string;
  code_ready: boolean;
  data_ready: boolean;
  python_available: boolean;
  bundled_seed_available: boolean;
  missing: string[];
};

export type Coverage = { dataset: string; instruments: number; collected: number; average_completeness: number };
export type LiveBatch = { status: string; run_id: string | null; asset_type: string | null; selected: number; completed: number; progress_ratio: number };
export type Candidate = { instrument_id: string; research_state: string; reason_codes: string[]; payload: Record<string, unknown> };
export type StrategyRun = { strategy_run_id: string; strategy: Strategy; release_id: string; screen: { screen_run_id: string; states: Record<string, number> } };

type CoreResponse<T> = { ok: boolean; result?: T; error?: { code: string; message: string } };

export async function core<T>(command: string, payload: Record<string, unknown> = {}): Promise<T> {
  const raw = await invoke<string>("invoke_core", { command, payloadJson: JSON.stringify(payload) });
  const response = JSON.parse(raw) as CoreResponse<T>;
  if (!response.ok || response.result === undefined) throw new Error(response.error?.message ?? "本地研究内核未返回结果");
  return response.result;
}

export function runtimeStatus(): Promise<RuntimeStatus> {
  return invoke<RuntimeStatus>("runtime_status");
}

export function bootstrapRuntime(): Promise<RuntimeStatus> {
  return invoke<RuntimeStatus>("bootstrap_runtime");
}
