import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;
  if (path.startsWith("/login") || path.startsWith("/api/login")) {
    return NextResponse.next();
  }
  const expected = process.env.ADMIN_SESSION_SECRET || "";
  const session = request.cookies.get("asts_session")?.value || "";
  if (!expected || session !== expected) {
    if (path.startsWith("/api/")) {
      return NextResponse.json({ detail: "authentication required" }, { status: 401 });
    }
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
