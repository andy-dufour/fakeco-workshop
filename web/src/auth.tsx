import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, session, setUnauthorizedHandler } from "./api";
import { setTelemetryPersona } from "./telemetry";
import type { AppConfig, Person, Role } from "./types";

interface Auth {
  user: Person | null;
  config: AppConfig | null;
  signIn: (id: string) => Promise<Person>;
  signOut: () => void;
}

const Ctx = createContext<Auth | null>(null);

export const homeFor: Record<Role, string> = {
  customer: "/",
  support: "/support",
  operations: "/ops",
  compliance: "/compliance",
};

export function AuthProvider({ config, children }: { config: AppConfig | null; children: ReactNode }) {
  const [user, setUser] = useState<Person | null>(() => session.user);
  setTelemetryPersona(user);

  const signOut = useCallback(() => {
    session.clear();
    setUser(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(signOut);
  }, [signOut]);

  // Check a token kept from earlier in this tab, and pick up account_id.
  useEffect(() => {
    if (!session.token) return;
    api
      .me()
      .then((me) => setUser((u) => ({ ...(u ?? me), ...me })))
      .catch(() => {});
  }, []);

  const signIn = useCallback(async (id: string) => {
    const res = await api.login(id);
    session.set(res.token, res.user);
    setUser(res.user);
    // /api/me adds account_id for customers.
    try {
      const me = await api.me();
      session.set(res.token, me);
      setUser(me);
      return me;
    } catch {
      return res.user;
    }
  }, []);

  const value = useMemo(() => ({ user, config, signIn, signOut }), [user, config, signIn, signOut]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
