/**
 * Sign-in client.
 *
 * The backend is the authority: POST /api/auth/login checks the email and
 * password against the accounts in backend/app/access.py and sets an httpOnly
 * cookie, which the browser then sends on EVERY same-origin request: API calls
 * and <img>/<video> media alike, so signing in once authorizes the whole app.
 *
 * localStorage holds the display profile only, so a refresh paints the app
 * immediately instead of waiting on a round trip. It never grants access: the
 * cookie does, and verifySession() re-checks it in the background.
 */

export interface AccountUser {
  email: string;
  name: string;
  /** One or two letters for the avatar circle. */
  initials: string;
}

const SESSION_KEY = "studio.session";

function isUser(value: unknown): value is AccountUser {
  const u = value as AccountUser | null;
  return (
    !!u &&
    typeof u.email === "string" &&
    typeof u.name === "string" &&
    typeof u.initials === "string"
  );
}

function remember(user: AccountUser): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(user));
  } catch {
    // Private mode: the profile just won't survive a refresh. The cookie will.
  }
}

function forget(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}

/** The cached profile, for painting the app before the session is re-checked. */
export function cachedUser(): AccountUser | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isUser(parsed)) {
      forget();
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

/** Sign in. Throws with the backend's message when the credentials don't match. */
export async function signIn(email: string, password: string): Promise<AccountUser> {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: email.trim(), password }),
  });
  if (!r.ok) {
    let detail = "That email and password don't match.";
    try {
      detail = (await r.json())?.detail || detail;
    } catch {
      /* keep default */
    }
    throw new Error(detail);
  }
  const user = (await r.json())?.user;
  if (!isUser(user)) throw new Error("Sign-in failed. Try again.");
  remember(user);
  return user;
}

/**
 * Re-check the session with the backend. Returns the signed-in person, or null
 * when the cookie is gone and the app should go back to the sign-in screen.
 *
 * Network trouble returns the cached profile rather than throwing anyone out:
 * the backend gate is the real protection, and it 401s every call regardless of
 * what this screen decides.
 */
export async function verifySession(): Promise<AccountUser | null> {
  try {
    const r = await fetch("/api/auth/status");
    if (!r.ok) return cachedUser();
    const s = (await r.json()) as {
      required?: boolean;
      authenticated?: boolean;
      user?: unknown;
    };
    if (s.required && !s.authenticated) {
      forget();
      return null;
    }
    // With the gate disarmed the backend reports no user, so the cached profile
    // stands in: there is nothing server-side to contradict it.
    const user = isUser(s.user) ? s.user : cachedUser();
    if (user) remember(user);
    return user;
  } catch {
    return cachedUser();
  }
}

/** Clear the local profile now and the cookie as soon as the backend answers. */
export function signOut(): void {
  forget();
  void fetch("/api/auth/logout", { method: "POST" }).catch(() => {
    // Offline: the cookie expires on its own, and the app is already locked.
  });
}
