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
 * Sign-in gate, with DEV BYPASS when backend is not running.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const devUser: AccountUser = { email: "dev@smaa.ai", name: "Developer", initials: "DV" };
  const [user, setUser] = useState<AccountUser | null>(() => cachedUser() ?? devUser);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    verifySession().then((verified) => {
      if (!cancelled && !verified) {
        setUser(devUser);
      }
    });
    return () => {
      cancelled = true;
    };
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
