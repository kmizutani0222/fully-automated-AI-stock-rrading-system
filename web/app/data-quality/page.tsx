"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Health = {
  symbol: string;
  provider: string;
  status: string;
  fetched_at: string;
  latest_bar_at: string | null;
  lag_seconds: number | null;
  gap_count: number;
  failure_count: number;
  message: string | null;
};
type Gaps = { gap_count: number };

function text(value: unknown) {
  if (value === null || value === undefined || value === "") return "計算不可";
  return String(value);
}

export default function DataQualityPage() {
  const [rows, setRows] = useState<Health[]>([]);
  const [gaps, setGaps] = useState<Gaps | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([api<Health[]>("/api/v1/market-data/health"), api<Gaps>("/api/v1/market-data/gaps?market=US&symbol=AAPL")])
      .then(([health, missing]) => {
        setRows(health);
        setGaps(missing);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);
  const latest = rows[0];
  return (
    <Frame>
      <h1>データ品質</h1>
      {error ? <p className="error">{error}</p> : null}
      <table>
        <tbody>
          <tr><th>最終取得時刻</th><td>{text(latest?.fetched_at)}</td></tr>
          <tr><th>最終確定足</th><td>{text(latest?.latest_bar_at)}</td></tr>
          <tr><th>データ遅延（秒）</th><td>{text(latest?.lag_seconds)}</td></tr>
          <tr><th>欠損足数</th><td>{gaps ? gaps.gap_count : text(latest?.gap_count)}</td></tr>
          <tr><th>取得失敗数</th><td>{text(latest?.failure_count)}</td></tr>
          <tr><th>データ提供元</th><td>{text(latest?.provider)}</td></tr>
        </tbody>
      </table>
      <h2>取得履歴</h2>
      <table>
        <thead><tr><th>銘柄</th><th>状態</th><th>取得時刻</th><th>提供元</th><th>メッセージ</th></tr></thead>
        <tbody>
          {rows.length === 0 ? <tr><td colSpan={5}>取得履歴はまだありません。</td></tr> : rows.slice(0, 20).map((row, index) => (
            <tr key={`${row.fetched_at}-${index}`}><td>{row.symbol}</td><td>{row.status}</td><td>{row.fetched_at}</td><td>{row.provider}</td><td>{row.message || "—"}</td></tr>
          ))}
        </tbody>
      </table>
    </Frame>
  );
}
