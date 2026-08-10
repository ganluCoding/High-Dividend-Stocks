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
  strategies: Strategy[];
  latest_runs: Record<string, { screen_run_id: string }>;
};

export type Coverage = { dataset: string; instruments: number; collected: number; average_completeness: number };
export type Candidate = { instrument_id: string; research_state: string; reason_codes: string[]; payload: Record<string, unknown> };
export type StrategyRun = { strategy_run_id: string; strategy: Strategy; release_id: string; screen: { screen_run_id: string; states: Record<string, number> } };

type CoreResponse<T> = { ok: boolean; result?: T; error?: { code: string; message: string } };

export async function core<T>(command: string, payload: Record<string, unknown> = {}): Promise<T> {
  const raw = await invoke<string>("invoke_core", { command, payloadJson: JSON.stringify(payload) });
  const response = JSON.parse(raw) as CoreResponse<T>;
  if (!response.ok || response.result === undefined) throw new Error(response.error?.message ?? "本地研究内核未返回结果");
  return response.result;
}
