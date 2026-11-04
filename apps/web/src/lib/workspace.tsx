"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { currentUser, signedIn, type SessionUser } from "@/lib/supabase";
import type { Tenant } from "@/lib/types";

const STORAGE_KEY = "covenant.tenant";
const WRITE_ROLES = new Set(["owner", "admin", "analyst"]);

type Workspace = {
  loading: boolean;
  error: string | null;
  user: SessionUser | null;
  tenants: Tenant[];
  /** The workspace being viewed: the chosen one, or the demo for anonymous visitors. */
  tenant: Tenant | null;
  setTenant: (id: string) => void;
  isDemo: boolean;
  /** The user can upload, decide matches and dispose alerts here. */
  canWrite: boolean;
  canManage: boolean;
  reload: () => void;
};

const WorkspaceContext = createContext<Workspace | null>(null);

function saved(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{
    loading: boolean;
    error: string | null;
    user: SessionUser | null;
    tenants: Tenant[];
  }>({ loading: true, error: null, user: null, tenants: [] });
  const [chosen, setChosen] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let live = true;
    (async () => {
      const sb = await signedIn();
      const user = await currentUser();
      const { data, error } = await sb.from("tenants").select("*").order("created_at");
      if (error) throw new Error(error.message);
      if (live) setState({ loading: false, error: null, user, tenants: (data ?? []) as Tenant[] });
    })().catch((e: unknown) => {
      if (live) setState((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e) }));
    });
    setChosen(saved());
    return () => {
      live = false;
    };
  }, [version]);

  const setTenant = useCallback((id: string) => {
    setChosen(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // private window: the choice lasts for this page only
    }
  }, []);

  const value = useMemo<Workspace>(() => {
    const tenant = state.tenants.find((t) => t.id === chosen) ?? state.tenants[0] ?? null;
    const role = tenant?.role ?? null;
    return {
      ...state,
      tenant,
      setTenant,
      isDemo: Boolean(tenant?.is_demo),
      canWrite: Boolean(state.user && !state.user.isAnonymous && role && WRITE_ROLES.has(role)),
      canManage: role === "owner" || role === "admin",
      reload: () => setVersion((v) => v + 1),
    };
  }, [state, chosen, setTenant]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): Workspace {
  const ws = useContext(WorkspaceContext);
  if (!ws) throw new Error("useWorkspace outside WorkspaceProvider");
  return ws;
}
