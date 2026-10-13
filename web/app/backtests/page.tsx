"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Run = { id: number; status: string; strategy: string; market: string; symbols: string[]; metrics: { total_pnl?: string; total_return?: string } | null; error: string | null };
type Point = { timestamp: string; equity: string | null; currency: string };

export default function BacktestsPage() {
  const [strategy, setStrategy] = useState("buy_hold");
  const [symbols, setSymbols] = useState("AAPL");
  const [cash, setCash] = useState("10000");
  const [runs, setRuns] = useState<Run[]>([]);
  const [equity, setEquity] = useState<Point[]>([]);
  const [message, setMessage] = useState("");

  async function load() {
    setRuns(await api<Run[]>("/api/v1/backtests"));
  }
  useEffect(() => { load().catch((reason: Error) => setMessage(reason.message)); }, []);

  return (
    <Frame>
      <h1>バックテスト</h1>
      <p className="muted">バックテスト口座はペーパー口座と分かれています。成績は保存済みデータだけで計算します。</p>
      <form
        className="row"
        onSubmit={async (event) => {
          event.preventDefault();
          setMessage("");
          try {
            const created = await api<{ id: number }>("/api/v1/backtests", {
              method: "POST",
              body: JSON.stringify({
                strategy,
                market: "US",
                symbols: symbols.split(",").map((item) => item.trim()).filter(Boolean),
                start: "2026-10-12T00:00:00Z",
                end: "2026-10-14T00:00:00Z",
                provider: "yfinance",
                initial_cash: { USD: cash },
              }),
            });
            setMessage(`実行 ${created.id} を受け付けました。`);
            await load();
          } catch (reason) {
            setMessage(reason instanceof Error ? reason.message : "失敗しました");
          }
        }}
      >
        <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
          <option value="ema_cross">EMA Cross</option>
          <option value="rsi_reversion">RSI</option>
          <option value="buy_hold">Buy & Hold</option>
        </select>
        <input value={symbols} onChange={(event) => setSymbols(event.target.value)} />
        <input value={cash} onChange={(event) => setCash(event.target.value)} />
        <button type="submit">実行</button>
      </form>
      {message ? <p>{message}</p> : null}
      <table>
        <thead><tr><th>ID</th><th>戦略</th><th>状態</th><th>損益</th><th>リターン</th><th></th></tr></thead>
        <tbody>
          {runs.slice(0, 12).map((run) => (
            <tr key={run.id}>
              <td>{run.id}</td>
              <td>{run.strategy}</td>
              <td>{run.status}</td>
              <td>{run.metrics?.total_pnl || "—"}</td>
              <td>{run.metrics?.total_return || run.error || "—"}</td>
              <td><button type="button" onClick={() => api<Point[]>(`/api/v1/backtests/${run.id}/equity`).then(setEquity)}>推移</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>{equity.length === 0 ? "資産推移は未選択です。" : equity.filter((row) => row.equity).slice(-6).map((row) => row.equity).join(" → ")}</p>
    </Frame>
  );
}
