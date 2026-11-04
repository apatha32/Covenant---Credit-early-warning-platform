"use client";

import { createClient } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

// Public values only: the project URL and the anon key. Every read goes through RLS-protected
// views in the `api` schema; the service-role key never reaches the browser.
const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

function makeClient(url: string, anonKey: string) {
  return createClient(url, anonKey, { db: { schema: "api" } });
}

export type ApiClient = ReturnType<typeof makeClient>;

let client: ApiClient | null = null;
let session: Promise<ApiClient> | null = null;

function getClient(): ApiClient {
  if (!url || !anonKey) {
    throw new Error("NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY are not set");
  }
  client ??= makeClient(url, anonKey);
  return client;
}

/** A client with a session: the saved one, or a new anonymous sign-in (read-only demo access). */
export function signedIn(): Promise<ApiClient> {
  session ??= (async () => {
    const sb = getClient();
    const { data } = await sb.auth.getSession();
    if (!data.session) {
      const { error } = await sb.auth.signInAnonymously();
      if (error) throw error;
    }
    return sb;
  })().catch((e) => {
    session = null; // allow a retry
    throw e;
  });
  return session;
}

export type Loadable<T> = { data: T | null; error: string | null; loading: boolean };

/** Run a query once the demo session exists; re-run when `deps` change. */
export function useQuery<T>(
  run: (sb: ApiClient) => PromiseLike<{ data: T | null; error: { message: string } | null }>,
  deps: unknown[],
): Loadable<T> {
  const [state, setState] = useState<Loadable<T>>({ data: null, error: null, loading: true });
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true }));
    signedIn()
      .then((sb) => run(sb))
      .then(({ data, error }) => {
        if (live) setState({ data, error: error ? error.message : null, loading: false });
      })
      .catch((e: unknown) => {
        if (live) setState({ data: null, error: e instanceof Error ? e.message : String(e), loading: false });
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

export type SessionUser = { id: string; email: string | null; isAnonymous: boolean };

/** Who is signed in: an account, or an anonymous demo visitor. */
export async function currentUser(): Promise<SessionUser | null> {
  const sb = await signedIn();
  const { data } = await sb.auth.getSession();
  const u = data.session?.user;
  return u ? { id: u.id, email: u.email ?? null, isAnonymous: Boolean(u.is_anonymous) } : null;
}

/** Account sign-in. Replaces the anonymous demo session. */
export async function signInWithPassword(email: string, password: string): Promise<void> {
  const sb = getClient();
  const { error } = await sb.auth.signInWithPassword({ email, password });
  if (error) throw error;
}

/** Returns true when the account is signed in right away, false when it must confirm by email. */
export async function signUp(email: string, password: string): Promise<boolean> {
  const sb = getClient();
  await sb.auth.signOut({ scope: "local" });
  const { data, error } = await sb.auth.signUp({ email, password });
  if (error) throw error;
  return Boolean(data.session);
}

/** Back to the anonymous demo. */
export async function signOut(): Promise<void> {
  await getClient().auth.signOut({ scope: "local" });
  session = null;
}

/** The current session's access token (for server routes that act as this visitor). */
export async function accessToken(): Promise<string> {
  const sb = await signedIn();
  const { data } = await sb.auth.getSession();
  if (!data.session) throw new Error("no session");
  return data.session.access_token;
}
