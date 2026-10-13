"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Instrument = { market: string; symbol: string; currency: string };
type Bar = { timestamp: string; open: number; high: number; low: number; close: number };
type Regime = { timestamp: string; regime: string; method: string };
type Prediction = { horizon: string; expected_return: string | null; is_out_of_sample: boolean; predicted_at: string };

export default function MarketPage() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [symbol, setSymbol] = useState("AAPL");
  const [market, setMarket] = useState("US");
  const [bars, setBars] = useState<Bar[]>([]);
  const [regimes, setRegimes] = useState<Regime[]>([]);
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Instrument[]>("/instruments").then(setInstruments).catch((reason: Error) => setError(reason.message));
    api<Regime[]>("/api/v1/ai/regimes").then(setRegimes).catch(() => setRegimes([]));
    api<Prediction[]>("/api/v1/ai/predictions").then(setPredictions).catch(() => setPredictions([]));
  }, []);

  useEffect(() => {
    api<Bar[]>(`/bars/${symbol}?market=${market}&limit=80`).then(setBars).catch((reason: Error) => setError(reason.message));
  }, [symbol, market]);

  const closes = bars.map((bar) => bar.close);
  const min = Math.min(...closes, 0);
  const max = Math.max(...closes, 1);
  const points = closes.map((close, index) => {
    const x = closes.length <= 1 ? 0 : (index / (closes.length - 1)) * 100;
    const y = max === min ? 50 : 100 - ((close - min) / (max - min)) * 100;
    return `${x},${y}`;
  });
  const latest = bars.at(-1);

  return (
    <Frame>
      <h1>市場監視</h1>
      <p className="muted">日本株のオンライン取得はありません。表示できるのは保存済みの5分足だけです。</p>
      {error ? <p className="error">{error}</p> : null}
      <div className="row">
        <select
          value={`${market}:${symbol}`}
          onChange={(event) => {
            const [nextMarket, nextSymbol] = event.target.value.split(":");
            setMarket(nextMarket);
            setSymbol(nextSymbol);
          }}
        >
          {instruments.map((item) => (
            <option key={`${item.market}:${item.symbol}`} value={`${item.market}:${item.symbol}`}>
              {item.market} {item.symbol}
            </option>
          ))}
        </select>
      </div>
      <div className="panel">
        <svg className="chart" viewBox="0 0 100 100" preserveAspectRatio="none">
          <polyline fill="none" stroke="#c6e27a" strokeWidth="1" points={points.join(" ")} />
        </svg>
        <p>終値 {latest ? latest.close : "—"} / 更新 {latest ? latest.timestamp : "データなし"}</p>
        <p>表示用EMA9 {ema(closes, 9)} / RSI14 {rsi(closes, 14)}</p>
        <p>相場環境 {regimes[0] ? `${regimes[0].regime} (${regimes[0].method})` : "未分類"}</p>
        <p>AI予測 {predictions.length === 0 ? "予測履歴はありません。モデルは未学習です。" : predictions.slice(0, 3).map((item) => `${item.horizon} ${item.expected_return}`).join(" / ")}</p>
      </div>
    </Frame>
  );
}

function ema(values: number[], period: number) {
  if (values.length < period) return "—";
  const weight = 2 / (period + 1);
  let current = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  for (const value of values.slice(period)) current = value * weight + current * (1 - weight);
  return current.toFixed(2);
}

function rsi(values: number[], period: number) {
  if (values.length <= period) return "—";
  let gain = 0;
  let loss = 0;
  for (let index = values.length - period; index < values.length; index += 1) {
    const delta = values[index] - values[index - 1];
    if (delta >= 0) gain += delta;
    else loss -= delta;
  }
  if (loss === 0) return "100";
  const rs = gain / loss;
  return (100 - 100 / (1 + rs)).toFixed(1);
}
