"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api, money } from "../lib/api";

type Account = { id: number; name: string; cash: Record<string, string>; mode: string };
type Position = { instrument_id: number; quantity: string; average_price: string; realized_pnl: string };
type Point = { timestamp: string; currency: string; equity: string | null; cash: string };

export default function PortfolioPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equity, setEquity] = useState<Point[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Account[]>("/api/v1/accounts")
      .then(async (rows) => {
        setAccounts(rows);
        const paper = rows.find((row) => row.name === "paper") || rows[0];
        if (!paper) return;
        const detail = await api<{ positions: Position[] }>(`/api/v1/accounts/${paper.id}`);
        setPositions(detail.positions || []);
        setEquity(await api<Point[]>(`/api/v1/dashboard/equity?account_id=${paper.id}`));
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);
  const paper = accounts.find((row) => row.name === "paper");
  const points = equity.filter((row) => row.currency === "USD" && row.equity);
  return (
    <Frame>
      <h1>ポートフォリオ</h1>
      {error ? <p className="error">{error}</p> : null}
      <div className="grid">
        <div className="card"><span>JPY</span><strong>{money(paper?.cash.JPY)}</strong></div>
        <div className="card"><span>USD</span><strong>{money(paper?.cash.USD)}</strong></div>
        <div className="card"><span>モード</span><strong>{paper?.mode || "—"}</strong></div>
      </div>
      <p className="muted">JPY と USD は自動換算しません。評価額は保存済みのスナップショットだけを使います。</p>
      <table>
        <thead><tr><th>銘柄ID</th><th>数量</th><th>平均取得</th><th>実現損益</th></tr></thead>
        <tbody>
          {positions.length === 0 ? <tr><td colSpan={4}>保有はありません。</td></tr> : positions.map((row) => (
            <tr key={row.instrument_id}><td>{row.instrument_id}</td><td>{row.quantity}</td><td>{row.average_price}</td><td>{row.realized_pnl}</td></tr>
          ))}
        </tbody>
      </table>
      <h2>資産推移</h2>
      <p>{points.length === 0 ? "資産スナップショットはまだありません。" : points.slice(-8).map((row) => `${row.timestamp} ${row.equity}`).join(" → ")}</p>
    </Frame>
  );
}
