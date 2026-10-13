import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "ASTS 管理画面",
  description: "ペーパートレードの運用画面",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
