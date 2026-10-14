"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Currency = { equity: string | null; day_pnl: string | null; max_drawdown: string | null };
type Report = {
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  trade_count: number | null;
  win_rate: string | null;
  cost_total: string | null;
  currencies: Record<string, Currency>;
};

function text(value: unknown) {
  if (value === null || value === undefined || value === "") return "計算不可";
  return String(value);
}

export default function PerformancePage() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<Report>("/api/v1/paper/performance").then(setReport).catch((reason: Error) => setError(reason.message));
  }, []);
  const currencies = Object.entries(report?.currencies || {});
  return (
    <Frame>
      <h1>成績</h1>
      {error ? <p className="error">{error}</p> : null}
      <p className="muted">取引が無い指標は計算不可です。通貨は合算しません。</p>
      <table>
        <thead><tr><th>通貨</th><th>総資産</th><th>日次損益</th><th>最大ドローダウン</th></tr></thead>
        <tbody>
          {currencies.length === 0 ? <tr><td colSpan={4}>スナップショットが無く、総資産は計算不可です。</td></tr> : currencies.map(([currency, row]) => (
            <tr key={currency}><td>{currency}</td><td>{text(row.equity)}</td><td>{text(row.day_pnl)}</td><td>{text(row.max_drawdown)}</td></tr>
          ))}
        </tbody>
      </table>
      <table>
        <tbody>
          <tr><th>実現損益</th><td>{text(report?.realized_pnl)}</td></tr>
          <tr><th>評価損益</th><td>{text(report?.unrealized_pnl)}</td></tr>
          <tr><th>取引回数</th><td>{text(report?.trade_count)}</td></tr>
          <tr><th>勝率</th><td>{text(report?.win_rate)}</td></tr>
          <tr><th>取引コスト</th><td>{text(report?.cost_total)}</td></tr>
        </tbody>
      </table>
    </Frame>
  );
}
