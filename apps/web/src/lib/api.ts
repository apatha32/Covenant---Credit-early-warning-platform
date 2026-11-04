"use client";

import { accessToken } from "@/lib/supabase";

// The Covenant API (FastAPI, services/api): every write and the company matcher. Public URL only;
// each call carries the signed-in user's Supabase access token.
const API_URL = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");

export const apiConfigured = Boolean(API_URL);

/** The free API host sleeps when idle; the first call after that can take up to a minute. */
export const WAKE_HINT =
  "The API runs on a free host that sleeps when idle, so the first request can take up to a minute.";

export async function api<T>(
  path: string,
  init: { method?: string; json?: unknown; form?: FormData } = {},
): Promise<T> {
  if (!API_URL) throw new Error("NEXT_PUBLIC_API_URL is not set, so uploads and decisions are off.");
  const headers: Record<string, string> = { Authorization: `Bearer ${await accessToken()}` };
  let body: BodyInit | undefined;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  } else if (init.form) {
    body = init.form;
  }
  const res = await fetch(`${API_URL}${path}`, {
    method: init.method ?? (body ? "POST" : "GET"),
    headers,
    body,
    signal: AbortSignal.timeout(120_000),
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail[0]?.msg : undefined;
    throw new Error(data?.error ?? detail ?? `API error ${res.status}`);
  }
  return data as T;
}

/** Wakes the API early (e.g. when the portfolio page opens) so the first real action is fast. */
export function wakeApi(): void {
  if (API_URL) fetch(`${API_URL}/health`).catch(() => undefined);
}
