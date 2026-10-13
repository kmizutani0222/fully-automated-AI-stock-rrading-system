"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

const links = [
  ["/", "ダッシュボード"],
  ["/market", "市場監視"],
  ["/portfolio", "ポートフォリオ"],
  ["/orders", "注文・約定"],
  ["/models", "AIモデル"],
  ["/backtests", "バックテスト"],
  ["/settings", "設定"],
];

export function Frame({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  return (
    <div className="shell">
      <nav>
        <strong>ASTS Paper</strong>
        {links.map(([href, label]) => (
          <Link key={href} href={href} className={path === href ? "active" : ""}>
            {label}
          </Link>
        ))}
        <button
          onClick={async () => {
            await fetch("/api/logout", { method: "POST" });
            router.push("/login");
          }}
        >
          ログアウト
        </button>
      </nav>
      <main>{children}</main>
    </div>
  );
}
