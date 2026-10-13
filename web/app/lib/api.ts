export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers || {}) },
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new Error(typeof body?.detail === "string" ? body.detail : text);
  }
  return body as T;
}

export function money(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}
