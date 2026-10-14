"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api, money } from "../lib/api";

type Status = {
  account_name: string;
  account_role: string;
  mode: string;
  cash: Record<string, string>;
  symbols: string[];
  strategies: string[];
  last_bar: string | null;
  last_signal_at: string | null;
  last_order_at: string | null;
  last_fill_at: string | null;
  order_count: number;
  fill_count: number;
  close_count: number;
  data_status: string;
  data_health: { symbol: string; status: string | null; fetched_at: string | null; failure_count: number | null }[];
  mode_changes: { previous_mode: string | null; new_mode: string; changed_at: string; actor: string; action: string; request_id: string | null; reason: string | null }[];
  latest_signal: { signal: string; reason: string; strategy: string; timestamp: string } | null;
  latest_decision: { choice: string; reason: string; timestamp: string } | null;
  latest_order: { id: number; side: string; quantity: string; status: string; reason: string | null } | null;
  latest_fill: { price: string; quantity: string; filled_at: string | null } | null;
  rejection_reason: string | null;
};
type Runtime = { trading_mode: string; live_trading_enabled: boolean };
type EmaPoint = {
  timestamp: string | null;
  close: string | null;
  bar_count: number;
  minimum_bars: number;
  sufficient: boolean;
  ema_fast: string | null;
  ema_slow: string | null;
  previous_ema_fast: string | null;
  previous_ema_slow: string | null;
  ema_fast_change: string | null;
  ema_slow_change: string | null;
  cross: string | null;
  signal: string | null;
  reason_text: string;
};
type EmaDiagnostic = {
  symbol: string;
  fast_period: number;
  slow_period: number;
  minimum_bars: number;
  bar_count: number;
  latest: EmaPoint | null;
  history: EmaPoint[];
};
type DailyReport = {
  account: string;
  role: string;
  day: string;
  bars_processed: number;
  signals: number;
  orders: number;
  fills: number;
  closes: number;
  rejections: number;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  cost_total: string | null;
  data_failures: number;
  scheduler_failures: number;
  profitability_evaluated: boolean;
  profitability_note: string;
};

export default function PaperPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [report, setReport] = useState<DailyReport | null>(null);
  const [ema, setEma] = useState<EmaDiagnostic | null>(null);
  const [message, setMessage] = useState("");

  async function reload() {
    const [paper, live, daily, diagnosis] = await Promise.all([
      api<Status>("/api/v1/paper/status"),
      api<Runtime>("/api/v1/runtime/status"),
      api<DailyReport>("/api/v1/paper/daily-report"),
      api<EmaDiagnostic>("/api/v1/paper/ema-diagnostic?symbol=AAPL&limit=48"),
    ]);
    setStatus(paper);
    setRuntime(live);
    setReport(daily);
    setEma(diagnosis);
  }
  useEffect(() => {
    reload().catch((reason: Error) => setMessage(reason.message));
  }, []);

  async function command(path: string, body: object, key?: string) {
    setMessage("");
    try {
      await api(path, { method: "POST", body: JSON.stringify(body), headers: key ? { "Idempotency-Key": key } : {} });
      await reload();
      setMessage("更新しました。");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "失敗しました");
    }
  }

  const role = status == null ? "読み込み中" : status.account_role === "operational" ? "運用口座" : "検証口座";

  return (
    <Frame>
      <h1>ペーパー運用</h1>
      <p className="muted">
        {role} {status?.account_name || "—"} / モード {runtime?.trading_mode || "—"} / 口座 {status?.mode || "—"} / LIVE {runtime?.live_trading_enabled ? "有効" : "禁止"}
      </p>
      <p className="muted">この画面は運用口座 paper だけを表示します。検証用の取引はテストデータベースにあり、ここの損益には入りません。</p>
      {message ? <p>{message}</p> : null}
      <table>
        <tbody>
          <tr><th>口座区分</th><td>{status ? `${role} ${status.account_name}` : "—"}</td></tr>
          <tr><th>仮想資金</th><td>{status ? Object.entries(status.cash).map(([currency, amount]) => `${currency} ${money(amount)}`).join(" / ") || "—" : "—"}</td></tr>
          <tr><th>対象銘柄</th><td>{status?.symbols.join(", ") || "—"}</td></tr>
          <tr><th>有効戦略</th><td>{status?.strategies.join(", ") || "—"}</td></tr>
          <tr><th>最終処理足</th><td>{status?.last_bar || "—"}</td></tr>
          <tr><th>最終シグナル時刻</th><td>{status?.last_signal_at || "—"}</td></tr>
          <tr><th>最終注文時刻</th><td>{status?.last_order_at || "—"}</td></tr>
          <tr><th>最終約定時刻</th><td>{status?.last_fill_at || "—"}</td></tr>
          <tr><th>注文・約定・決済</th><td>{status ? `${status.order_count} / ${status.fill_count} / ${status.close_count}` : "—"}</td></tr>
          <tr><th>データ取得</th><td>{status ? `${status.data_status}${status.data_health.map((item) => ` ${item.symbol}:${item.status || "未取得"}`).join("")}` : "—"}</td></tr>
          <tr><th>最新シグナル</th><td>{status?.latest_signal ? `${status.latest_signal.strategy} ${status.latest_signal.signal} ${status.latest_signal.reason}` : "—"}</td></tr>
          <tr><th>最新判断</th><td>{status?.latest_decision ? `${status.latest_decision.choice} ${status.latest_decision.reason}` : "—"}</td></tr>
          <tr><th>最新注文</th><td>{status?.latest_order ? `#${status.latest_order.id} ${status.latest_order.side} ${status.latest_order.quantity} ${status.latest_order.status}` : "—"}</td></tr>
          <tr><th>最新約定</th><td>{status?.latest_fill ? `${status.latest_fill.price} x ${status.latest_fill.quantity}` : "—"}</td></tr>
          <tr><th>注文拒否理由</th><td>{status?.rejection_reason || "—"}</td></tr>
        </tbody>
      </table>
      <h2>EMA診断</h2>
      <p className="muted">読み取り専用です。売買判断、注文、残高は変えません。</p>
      <table>
        <tbody>
          <tr><th>対象</th><td>{ema ? `${ema.symbol} 5分足 / 確定足 ${ema.bar_count}本 / 必要 ${ema.minimum_bars}本` : "—"}</td></tr>
          <tr><th>最新足</th><td>{ema?.latest?.timestamp || "—"}</td></tr>
          <tr><th>EMA{ema?.fast_period || 9}</th><td>{ema?.latest ? `${money(ema.latest.ema_fast)}（前回 ${money(ema.latest.previous_ema_fast)} / 差 ${money(ema.latest.ema_fast_change)}）` : "—"}</td></tr>
          <tr><th>EMA{ema?.slow_period || 21}</th><td>{ema?.latest ? `${money(ema.latest.ema_slow)}（前回 ${money(ema.latest.previous_ema_slow)} / 差 ${money(ema.latest.ema_slow_change)}）` : "—"}</td></tr>
          <tr><th>クロス</th><td>{ema?.latest ? (ema.latest.cross === "up" ? "上抜け" : ema.latest.cross === "down" ? "下抜け" : ema.latest.sufficient ? "なし" : "判定不可") : "—"}</td></tr>
          <tr><th>判定理由</th><td>{ema?.latest?.reason_text || "—"}</td></tr>
        </tbody>
      </table>
      <table>
        <thead>
          <tr><th>足</th><th>本数</th><th>EMA{ema?.fast_period || 9}</th><th>EMA{ema?.slow_period || 21}</th><th>前回EMA{ema?.fast_period || 9}</th><th>前回EMA{ema?.slow_period || 21}</th><th>クロス</th><th>理由</th></tr>
        </thead>
        <tbody>
          {ema?.history.length ? ema.history.map((row) => (
            <tr key={row.timestamp || "none"}>
              <td>{row.timestamp || "—"}</td>
              <td>{row.sufficient ? row.bar_count : `${row.bar_count} / データ不足`}</td>
              <td>{money(row.ema_fast)}</td>
              <td>{money(row.ema_slow)}</td>
              <td>{money(row.previous_ema_fast)}</td>
              <td>{money(row.previous_ema_slow)}</td>
              <td>{row.cross === "up" ? "上抜け" : row.cross === "down" ? "下抜け" : row.sufficient ? "なし" : "判定不可"}</td>
              <td>{row.reason_text}</td>
            </tr>
          )) : <tr><td colSpan={8}>診断履歴はありません</td></tr>}
        </tbody>
      </table>
      <h2>日次レポート</h2>
      <p className="muted">{report ? `${report.day} / ${report.profitability_note}` : "—"}</p>
      <table>
        <tbody>
          <tr><th>処理した5分足</th><td>{report ? report.bars_processed : "—"}</td></tr>
          <tr><th>シグナル</th><td>{report ? report.signals : "—"}</td></tr>
          <tr><th>注文</th><td>{report ? report.orders : "—"}</td></tr>
          <tr><th>約定</th><td>{report ? report.fills : "—"}</td></tr>
          <tr><th>決済</th><td>{report ? report.closes : "—"}</td></tr>
          <tr><th>注文拒否</th><td>{report ? report.rejections : "—"}</td></tr>
          <tr><th>実現損益</th><td>{money(report?.realized_pnl)}</td></tr>
          <tr><th>評価損益</th><td>{money(report?.unrealized_pnl)}</td></tr>
          <tr><th>取引コスト</th><td>{money(report?.cost_total)}</td></tr>
          <tr><th>データ取得失敗</th><td>{report ? report.data_failures : "—"}</td></tr>
          <tr><th>スケジューラー失敗</th><td>{report ? report.scheduler_failures : "—"}</td></tr>
        </tbody>
      </table>
      <h2>モード変更履歴</h2>
      <table>
        <thead>
          <tr><th>時刻</th><th>変更前</th><th>変更後</th><th>操作</th><th>実行者</th><th>理由</th></tr>
        </thead>
        <tbody>
          {status?.mode_changes.length ? status.mode_changes.map((row) => (
            <tr key={`${row.changed_at}-${row.action}-${row.new_mode}`}>
              <td>{row.changed_at}</td>
              <td>{row.previous_mode || "—"}</td>
              <td>{row.new_mode}</td>
              <td>{row.action}</td>
              <td>{row.actor}</td>
              <td>{row.reason || "—"}</td>
            </tr>
          )) : <tr><td colSpan={6}>履歴はありません</td></tr>}
        </tbody>
      </table>
      <p>
        <button onClick={() => command("/api/v1/runtime/pause", { account_id: 1 })}>新規注文を停止</button>
        <button onClick={() => command("/api/v1/runtime/resume", { account_id: 1 })}>再開</button>
        <button onClick={() => command("/api/v1/paper/deposits", { account_id: 1, currency: "USD", amount: "10000" }, `usd-${crypto.randomUUID()}`)}>USD 10000 を仮想入金</button>
      </p>
    </Frame>
  );
}
