import { useState } from "react";

import { signIn, type AccountUser } from "../../lib/auth";

/**
 * The app's first screen: email + password, checked by the backend against the
 * accounts in app/access.py. Mounts above <App/>, outside ToastProvider, so a
 * bad sign-in reports itself as inline text.
 *
 * Split layout: brand panel left, form right. The panel folds away below lg and
 * a small lockup tops the form instead.
 */
export function SignInScreen({ onSignedIn }: { onSignedIn: (user: AccountUser) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !email.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(email, password));
    } catch (err: any) {
      setError(err?.message || "That email and password don't match.");
      setBusy(false);
    }
    // On success the gate swaps this screen out, so there is nothing to reset.
  }

  return (
    <div className="min-h-screen bg-bg lg:grid lg:grid-cols-2">
      {/* Brand panel */}
      <div className="relative hidden lg:flex flex-col items-center justify-center gap-5 bg-fg overflow-hidden">
        {["0s", "1s", "2s"].map((animationDelay) => (
          <span
            key={animationDelay}
            aria-hidden
            className="absolute w-80 h-80 rounded-full border border-accent/30 pointer-events-none animate-haloPulse motion-reduce:animate-none motion-reduce:opacity-20"
            style={{ animationDelay }}
          />
        ))}
        <span
          aria-hidden
          className="absolute w-56 h-56 rounded-full bg-accent/10 blur-3xl pointer-events-none"
        />
        {/* The real 42 mark, same asset as the sidebar. */}
        <img
          src="/42-logo.png"
          alt=""
          className="relative w-28 xl:w-36 object-contain"
        />
        <p className="relative text-sm text-white/60">Creative Command Center</p>
      </div>

      {/* Form */}
      <div className="flex min-h-screen items-center justify-center px-4 py-12">
        <form onSubmit={submit} className="w-full max-w-sm space-y-7">
          <div className="flex items-center gap-2.5 lg:hidden">
            <span className="inline-flex w-8 h-8 items-center justify-center rounded bg-fg p-1.5">
              <img src="/42-logo.png" alt="" className="h-full w-full object-contain" />
            </span>
            <span className="text-lg font-bold tracking-tight text-fg">42 Studio</span>
          </div>

          <div className="space-y-1.5">
            <h1 className="text-2xl font-bold tracking-tight text-fg">Sign in</h1>
            <p className="text-sm text-muted">Use your 42 Studio account.</p>
          </div>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="signin-email" className="text-xs font-medium text-fg">
                Email
              </label>
              <input
                id="signin-email"
                type="email"
                autoFocus
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@smaa.com"
                className="input w-full"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="signin-password" className="text-xs font-medium text-fg">
                Password
              </label>
              <input
                id="signin-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input w-full"
              />
            </div>
            {error && (
              <p className="text-xs text-danger" role="alert">
                {error}
              </p>
            )}
          </div>

          <button
            type="submit"
            disabled={busy || !email.trim() || !password}
            className="btn btn-primary w-full"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
