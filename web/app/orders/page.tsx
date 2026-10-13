"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Order = { id: number; side: string; order_type: string; quantity: string; filled_quantity: string; status: string; backtest_run_id: number | null };
type Trade = { symbol: string; side: string; quantity: string; price: string; commission: string; slippage_cost: string; spread_cost: string; executed_at: string; status: string };
type Risk = { id: number; code: string; message: string };

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [risks, setRisks] = useState<Risk[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([
      api<Order[]>("/api/v1/orders"),
      api<Trade[]>("/api/v1/dashboard/trades"),
      api<Risk[]>("/api/v1/risk/events"),
    ]).then(([orderRows, tradeRows, riskRows]) => {
      setOrders(orderRows);
      setTrades(tradeRows);
      setRisks(riskRows);
    }).catch((reason: Error) => setError(reason.message));
  }, []);
  const paperOrders = orders.filter((order) => order.backtest_run_id === null);
  return (
    <Frame>
      <h1>注文・約定</h1>
      {error ? <p className="error">{error}</p> : null}
      <h2>ペーパー注文</h2>
      <table>
        <thead><tr><th>ID</th><th>売買</th><th>種別</th><th>数量</th><th>約定数量</th><th>状態</th></tr></thead>
        <tbody>
          {paperOrders.length === 0 ? <tr><td colSpan={6}>ペーパー注文はまだありません。</td></tr> : paperOrders.slice(0, 30).map((order) => (
            <tr key={order.id}><td>{order.id}</td><td>{order.side}</td><td>{order.order_type}</td><td>{order.quantity}</td><td>{order.filled_quantity}</td><td>{order.status}</td></tr>
          ))}
        </tbody>
      </table>
      <h2>約定</h2>
      <table>
        <thead><tr><th>銘柄</th><th>売買</th><th>数量</th><th>価格</th><th>手数料</th><th>スリッページ</th><th>スプレッド</th></tr></thead>
        <tbody>
          {trades.length === 0 ? <tr><td colSpan={7}>約定履歴はまだありません。</td></tr> : trades.slice(0, 30).map((trade, index) => (
            <tr key={`${trade.executed_at}-${index}`}><td>{trade.symbol}</td><td>{trade.side}</td><td>{trade.quantity}</td><td>{trade.price}</td><td>{trade.commission}</td><td>{trade.slippage_cost}</td><td>{trade.spread_cost}</td></tr>
          ))}
        </tbody>
      </table>
      <h2>拒否・リスク</h2>
      <table>
        <thead><tr><th>コード</th><th>理由</th></tr></thead>
        <tbody>
          {risks.slice(0, 15).map((row) => <tr key={row.id}><td>{row.code}</td><td>{row.message}</td></tr>)}
        </tbody>
      </table>
    </Frame>
  );
}
