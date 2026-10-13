"use client";

import { useEffect, useState } from "react";
import { Frame } from "./components/Frame";
import { api, money } from "./lib/api";

type Summary = {
  mode: string;
  scheduler: string | null;
  cash: Record<string, string>;
  equity: Record<string, string | null>;
  day_pnl: Record<string, string> | null;
  realized_pnl: string | null;
  max_drawdown: string | null;
  open_positions: number;
  active_models: number;
  latest_decision: { choice: string; reason: string; timestamp: string } | null;
  latest_orders: { id: number; side: string; status: string; quantity: string }[];
  fill_model: string;
};

export default function DashboardPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Summary>("/api/v1/dashboard/summary").then(setSummary).catch((reason: Error) => setError(reason.message));
  }, []);
  return (
    <Frame>
      <h1>ダッシュボード</h1>
      <p className="muted">PAPER のみです。LIVE は起動しません。</p>
      {error ? <p className="error">{error}</p> : null}
      {summary ? (
        <>
          <div className="grid">
            <div className="card"><span>現金 JPY</span><strong>{money(summary.cash.JPY)}</strong></div>
            <div className="card"><span>現金 USD</span><strong>{money(summary.cash.USD)}</strong></div>
            <div className="card"><span>評価 USD</span><strong>{money(summary.equity.USD)}</strong></div>
            <div className="card"><span>実現損益</span><strong>{money(summary.realized_pnl)}</strong></div>
            <div className="card"><span>当日損益</span><strong>{summary.day_pnl ? Object.entries(summary.day_pnl).map(([key, value]) => `${key} ${value}`).join(" / ") || "—" : "—"}</strong></div>
            <div className="card"><span>最大ドローダウン</span><strong>{money(summary.max_drawdown)}</strong></div>
            <div className="card"><span>保有銘柄数</span><strong>{summary.open_positions}</strong></div>
            <div className="card"><span>稼働</span><strong>{summary.scheduler || "未起動"} / {summary.mode}</strong></div>
          </div>
          <div className="panel">
            <p>有効モデル {summary.active_models} 件。{summary.active_models === 0 ? "学習済みモデルはまだ有効化されていません。" : "有効なモデルがあります。"}</p>
            <p>最新判断: {summary.latest_decision ? `${summary.latest_decision.choice} / ${summary.latest_decision.reason}` : "まだありません"}</p>
            <p className="muted">{summary.fill_model}</p>
          </div>
          <h2>最新の注文</h2>
          <table>
            <thead><tr><th>ID</th><th>売買</th><th>数量</th><th>状態</th></tr></thead>
            <tbody>
              {summary.latest_orders.length === 0 ? <tr><td colSpan={4}>ペーパー注文はまだありません。</td></tr> : summary.latest_orders.map((order) => (
                <tr key={order.id}><td>{order.id}</td><td>{order.side}</td><td>{order.quantity}</td><td>{order.status}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </Frame>
  );
}
