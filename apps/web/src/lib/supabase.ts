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
