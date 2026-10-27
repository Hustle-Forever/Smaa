import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { cachedUser, signOut, verifySession, type AccountUser } from "../../lib/auth";
import { SignInScreen } from "./SignInScreen";

interface AuthContextValue {
  user: AccountUser;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** The signed-in person. Throws outside AuthGate, like useToast does. */
export function useCurrentUser(): AccountUser {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useCurrentUser must be used inside AuthGate");
  return ctx.user;
}

/** Ends the session and returns to the sign-in screen. Throws outside AuthGate. */
export function useSignOut(): () => void {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useSignOut must be used inside AuthGate");
  return ctx.signOut;
}

/**
 * Sign-in → app. Wraps the whole app.
 *
 * The first render comes from the cached profile in a lazy initializer, never
 * an effect: an effect would paint the sign-in screen first and then swap, so
 * every refresh would flash it before the dashboard (and React.StrictMode
 * double-invokes effects). The backend is then asked whether that session is
 * still real, and only a definite "no" sends the person back to sign in.
 *
 * This is the UX. The real protection is the backend middleware, which 401s
 * every paid /api call without the cookie, so tokens are safe even if this
 * screen is bypassed.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AccountUser | null>(() => cachedUser());

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    verifySession().then((verified) => {
      if (!cancelled && !verified) setUser(null);
    });
    return () => {
      cancelled = true;
    };
    // Runs on mount and again after a fresh sign-in, which is cheap and keeps
    // the cookie and this screen from drifting apart.
  }, [user?.email]);

  const handleSignOut = useCallback(() => {
    signOut();
    setUser(null);
  }, []);

  if (!user) return <SignInScreen onSignedIn={setUser} />;

  return (
    <AuthContext.Provider value={{ user, signOut: handleSignOut }}>
      {children}
    </AuthContext.Provider>
  );
}
