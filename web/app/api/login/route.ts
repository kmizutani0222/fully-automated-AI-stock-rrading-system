import { NextResponse } from "next/server";

export async function POST(request: Request) {
  const body = (await request.json()) as { password?: string };
  const password = process.env.ADMIN_PASSWORD || "";
  const secret = process.env.ADMIN_SESSION_SECRET || "";
  if (!password || !secret || body.password !== password) {
    return NextResponse.json({ detail: "authentication required" }, { status: 401 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set("asts_session", secret, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: false,
  });
  return response;
}
