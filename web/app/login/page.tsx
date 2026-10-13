"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  return (
    <form
      className="login panel"
      onSubmit={async (event) => {
        event.preventDefault();
        const response = await fetch("/api/login", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ password }),
        });
        if (!response.ok) {
          setError("パスワードが違います。");
          return;
        }
        router.push("/");
      }}
    >
      <h1>管理画面</h1>
      <p className="muted">ペーパー運用の確認だけができます。実売買の鍵はこの画面に出しません。</p>
      <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="パスワード" />
      <div className="row">
        <button type="submit">入る</button>
      </div>
      {error ? <p className="error">{error}</p> : null}
    </form>
  );
}
