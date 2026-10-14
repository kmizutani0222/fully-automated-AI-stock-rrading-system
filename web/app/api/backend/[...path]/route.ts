import { NextRequest, NextResponse } from "next/server";

async function forward(request: NextRequest, path: string[]) {
  const base = process.env.API_BASE_URL || "http://app:8000";
  const target = new URL(`${base}/${path.join("/")}`);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  const token = process.env.ADMIN_TOKEN || "";
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }
  if (request.headers.get("content-type")) {
    headers.set("content-type", request.headers.get("content-type") || "application/json");
  }
  const idempotency = request.headers.get("idempotency-key");
  if (idempotency) {
    headers.set("idempotency-key", idempotency);
  }
  const init: RequestInit = { method: request.method, headers };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }
  const response = await fetch(target, init);
  const text = await response.text();
  return new NextResponse(text, {
    status: response.status,
    headers: { "content-type": response.headers.get("content-type") || "application/json" },
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return forward(request, path);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return forward(request, path);
}
