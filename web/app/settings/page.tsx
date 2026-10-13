"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Account = { id: number; name: string; mode: string; symbols: string[]; strategies: string[]; fallback_to_rules: boolean; cash: Record<string, string> };
type Status = { scheduler: string | null; account_mode: string | null; jp_auto_trading: boolean; fill_model: string };

export default function SettingsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [name, setName] = useState("desk");
  const [usd, setUsd] = useState("10000");
  const [message, setMessage] = useState("");

  async function reload() {
    const [accountRows, runtime] = await Promise.all([api<Account[]>("/api/v1/accounts"), api<Status>("/api/v1/runtime/status")]);
    setAccounts(accountRows);
    setStatus(runtime);
  }
  useEffect(() => { reload().catch((reason: Error) => setMessage(reason.message)); }, []);
  const paper = accounts.find((account) => account.name === "paper");

  async function command(path: string, body: object) {
    setMessage("");
    try {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      await reload();
      setMessage("更新しました。");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "失敗しました");
    }
  }

  return (
    <Frame>
      <h1>設定</h1>
      <p className="muted">スケジューラ {status?.scheduler || "未起動"} / 口座モード {status?.account_mode || "—"} / 日本株の自動売買 {status?.jp_auto_trading ? "有効" : "無効"}</p>
      <p className="muted">{status?.fill_model}</p>
      {message ? <p>{message}</p> : null}
      <div className="row">
        <button onClick={() => paper && command("/api/v1/runtime/start", { account_id: paper.id, mode: "RULE_BASED" })}>ルールで開始</button>
        <button onClick={() => paper && command("/api/v1/runtime/start", { account_id: paper.id, mode: "AI" })}>AIで開始</button>
        <button onClick={() => paper && command("/api/v1/runtime/start", { account_id: paper.id, mode: "HYBRID" })}>ハイブリッドで開始</button>
        <button className="danger" onClick={() => paper && command("/api/v1/runtime/pause", { account_id: paper.id })}>新規注文を停止</button>
        <button onClick={() => paper && command("/api/v1/runtime/resume", { account_id: paper.id })}>再開</button>
      </div>
      <h2>ペーパー口座</h2>
      <table>
        <thead><tr><th>名前</th><th>モード</th><th>JPY</th><th>USD</th><th>フォールバック</th></tr></thead>
        <tbody>
          {accounts.map((account) => (
            <tr key={account.id}><td>{account.name}</td><td>{account.mode}</td><td>{account.cash.JPY || "0"}</td><td>{account.cash.USD || "0"}</td><td>{account.fallback_to_rules ? "許可" : "不許可"}</td></tr>
          ))}
        </tbody>
      </table>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          command("/api/v1/accounts", { name, mode: "PAUSED", usd_cash: usd, jpy_cash: "1000000", strategies: ["ema_cross"], symbols: ["AAPL"], markets: ["US"], fallback_to_rules: false });
        }}
      >
        <input value={name} onChange={(event) => setName(event.target.value)} />
        <input value={usd} onChange={(event) => setUsd(event.target.value)} />
        <button type="submit">口座を作る</button>
      </form>
      {paper ? (
        <div className="row">
          <button onClick={() => command(`/api/v1/accounts/${paper.id}/cash`, { currency: "USD", amount: "10000", reason: "DEPOSIT" })}>USD を入金</button>
          <button onClick={() => command(`/api/v1/accounts/${paper.id}/settings`, { fallback_to_rules: true, strategies: ["ema_cross"], symbols: ["AAPL"] })}>ルールへのフォールバックを許可</button>
        </div>
      ) : null}
    </Frame>
  );
}
