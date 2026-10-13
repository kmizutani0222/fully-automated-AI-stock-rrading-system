"use client";

import { useEffect, useState } from "react";
import { Frame } from "../components/Frame";
import { api } from "../lib/api";

type Model = { id: number; task: string; version: string; status: string; train_end: string | null; test_start: string | null };
type Run = { id: number; task: string; status: string; reason: string | null };
type Prediction = { horizon: string; expected_return: string | null; is_out_of_sample: boolean };
type Decision = { choice: string; reason: string; timestamp: string };

export default function ModelsPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([
      api<Model[]>("/api/v1/ai/models"),
      api<Run[]>("/api/v1/ai/training-runs"),
      api<Prediction[]>("/api/v1/ai/predictions"),
      api<Decision[]>("/api/v1/ai/decisions"),
    ]).then(([modelRows, runRows, predictionRows, decisionRows]) => {
      setModels(modelRows);
      setRuns(runRows);
      setPredictions(predictionRows);
      setDecisions(decisionRows);
    }).catch((reason: Error) => setError(reason.message));
  }, []);
  const active = models.filter((model) => model.status === "ACTIVE");
  return (
    <Frame>
      <h1>AIモデル</h1>
      {error ? <p className="error">{error}</p> : null}
      {active.length === 0 ? <p className="error">有効な学習済みモデルはありません。AIモードでは新規注文を出しません。</p> : <p className="ok">有効モデル {active.length} 件</p>}
      <h2>モデル</h2>
      <table>
        <thead><tr><th>ID</th><th>タスク</th><th>版</th><th>状態</th><th>学習終了</th><th>テスト開始</th></tr></thead>
        <tbody>
          {models.length === 0 ? <tr><td colSpan={6}>モデルは未作成です。</td></tr> : models.map((model) => (
            <tr key={model.id}><td>{model.id}</td><td>{model.task}</td><td>{model.version}</td><td>{model.status}</td><td>{model.train_end || "—"}</td><td>{model.test_start || "—"}</td></tr>
          ))}
        </tbody>
      </table>
      <h2>学習</h2>
      <table>
        <thead><tr><th>ID</th><th>タスク</th><th>状態</th><th>理由</th></tr></thead>
        <tbody>
          {runs.slice(0, 10).map((run) => <tr key={run.id}><td>{run.id}</td><td>{run.task}</td><td>{run.status}</td><td>{run.reason}</td></tr>)}
        </tbody>
      </table>
      <h2>予測と戦略選択</h2>
      <p>{predictions.length === 0 ? "予測履歴はありません。" : `直近の予測 ${predictions.length} 件`}</p>
      <p>{decisions[0] ? `最新選択 ${decisions[0].choice} / ${decisions[0].reason}` : "戦略選択の履歴はまだありません。"}</p>
    </Frame>
  );
}
