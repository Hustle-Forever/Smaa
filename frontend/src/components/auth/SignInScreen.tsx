import { useRef, useState } from "react";
import { AlertTriangle, ArrowRight, Eye, EyeOff, Loader2, Lock, Mail, Moon, Sun } from "lucide-react";

import { signIn, type AccountUser } from "../../lib/auth";
import { currentTheme, setTheme, type StudioTheme } from "../../lib/theme";
import { MapBackdrop } from "../k42/MapBackdrop";

/**
 * The app's first screen: email + password, checked by the backend against the
 * accounts in app/access.py. Mounts above <App/>, outside ToastProvider, so a
 * bad sign-in reports itself as inline text.
 *
 * Layout (42abudhabi.ae branding, both themes):
 *  - left: a charcoal brand panel in BOTH themes (like the sidebar) with the
 *    animated cyan city map, the 42 Studio lockup, one headline and the three
 *    steps of the studio. On phones it folds into a short band on top.
 *  - right: the form on the page colour, with a Dark / Light switch.
 *
 * The sign-in call, its fields and its error text are unchanged; only the
 * look, and small helpers (show password, Caps Lock warning), are new.
 */
export function SignInScreen({ onSignedIn }: { onSignedIn: (user: AccountUser) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showPw, setShowPw] = useState(false);
  const [caps, setCaps] = useState(false);
  // Bumped on every failed attempt so the shake replays.
  const [attempt, setAttempt] = useState(0);
  const [theme, setThemeState] = useState<StudioTheme>(currentTheme());
  const emailRef = useRef<HTMLInputElement | null>(null);
  const pwRef = useRef<HTMLInputElement | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!email.trim() || !password) {
      setError(!email.trim() ? "Type your email first." : "Type your password.");
      setAttempt((n) => n + 1);
      (!email.trim() ? emailRef : pwRef).current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(email, password));
    } catch (err: any) {
      setError(err?.message || "That email and password don't match.");
      setAttempt((n) => n + 1);
      setBusy(false);
      pwRef.current?.select();
    }
    // On success the gate swaps this screen out, so there is nothing to reset.
  }

  const pickTheme = (t: StudioTheme) => {
    setTheme(t);
    setThemeState(t);
  };
  const capsCheck = (e: React.KeyboardEvent<HTMLInputElement>) => setCaps(e.getModifierState?.("CapsLock") ?? false);

  return (
    <div className="min-h-screen bg-k-canvas lg:grid lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      {/* ---------- Brand panel (charcoal in both themes) ---------- */}
      <aside className="relative overflow-hidden bg-k-rail text-white h-56 sm:h-64 lg:h-auto lg:min-h-screen">
        <MapBackdrop variant="panel" />
        {/* Cyan rule along the top, as on the sidebar and 42abudhabi.ae. */}
        <span className="absolute top-0 left-0 right-0 h-[3px] bg-k-cyan" aria-hidden />

        <div className="relative h-full flex flex-col justify-between px-6 py-6 sm:px-10 sm:py-8 lg:px-14 lg:py-12">
          {/* Lockup */}
          <div className="flex items-center gap-3 k-rise" style={{ animationDelay: "80ms" }}>
            <img src="/42-logo.png" alt="42" className="h-9 lg:h-11 w-auto object-contain" />
            <span className="leading-none">
              <span className="block k-display text-[18px] lg:text-[22px] text-white">Studio</span>
              <span className="block mt-1 text-[10.5px] font-bold uppercase tracking-[0.16em] text-white/55">Abu Dhabi</span>
            </span>
          </div>

          {/* Headline + the three steps */}
          <div className="max-w-[520px] space-y-5 lg:space-y-8">
            <div className="space-y-3">
              <p className="hidden sm:block text-[11.5px] font-bold uppercase tracking-[0.16em] text-k-cyan k-rise" style={{ animationDelay: "160ms" }}>
                The content studio of 42 Abu Dhabi
              </p>
              <h2
                className="k-display text-[24px] sm:text-[30px] lg:text-[44px] leading-[1.05] text-white text-balance k-rise"
                style={{ animationDelay: "240ms" }}
              >
                <span className="text-k-cyan" aria-hidden>&gt; </span>
                Make posts that look and sound like{" "}
                <span className="text-k-cyan whitespace-nowrap">
                  <span className="k-pixel" aria-hidden>&lt;</span>42<span className="k-cursor" aria-hidden>_</span>
                  <span className="k-pixel" aria-hidden>/&gt;</span>
                </span>
              </h2>
            </div>
            <ol className="hidden lg:flex flex-wrap items-center gap-2 text-[12.5px]" aria-label="How the studio works">
              {["Pick a tool", "AI makes it", "You press Post"].map((step, i) => (
                <li key={step} className="flex items-center gap-2 k-rise" style={{ animationDelay: `${360 + i * 110}ms` }}>
                  <span className="inline-flex items-center gap-2.5 h-9 pl-1 pr-3.5 bg-k-rail ring-1 ring-white/20">
                    <span className="w-7 h-7 inline-flex items-center justify-center bg-k-cyan text-k-oncyan text-[12px] font-bold tabular-nums">
                      {i + 1}
                    </span>
                    <span className="font-semibold text-white/90">{step}</span>
                  </span>
                  {i < 2 && <ArrowRight className="w-3.5 h-3.5 text-white/40" strokeWidth={2} aria-hidden />}
                </li>
              ))}
            </ol>
          </div>

          <p className="hidden lg:block text-[12px] text-white/45 k-rise" style={{ animationDelay: "720ms" }}>
            Posters, videos and captions in the 42 style, ready for Instagram.
          </p>
        </div>
      </aside>

      {/* ---------- Form ---------- */}
      <main className="relative flex lg:min-h-screen items-start lg:items-center justify-center px-5 sm:px-8 py-10 lg:py-16">
        {/* Theme switch */}
        <div
          className="absolute top-4 right-4 sm:top-6 sm:right-6 inline-flex p-0.5 bg-k-inset ring-1 ring-k-border"
          role="group"
          aria-label="Colour theme"
        >
          {(["dark", "light"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => pickTheme(t)}
              aria-pressed={theme === t}
              className={[
                "h-8 px-2.5 inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.06em] transition-colors",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn",
                theme === t ? "bg-k-btn text-k-onbtn" : "text-k-text/60 hover:text-k-text",
              ].join(" ")}
            >
              {t === "dark" ? <Moon className="w-3.5 h-3.5" strokeWidth={2} aria-hidden /> : <Sun className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />}
              {t === "dark" ? "Dark" : "Light"}
            </button>
          ))}
        </div>

        <form onSubmit={submit} noValidate className="w-full max-w-[400px] pt-6 lg:pt-0" aria-describedby={error ? "signin-error" : undefined}>
          <div className="space-y-2 k-rise" style={{ animationDelay: "120ms" }}>
            <p className="text-[12px] font-bold uppercase tracking-[0.14em] text-k-text/55">Welcome back</p>
            <h1 className="k-display text-[34px] sm:text-[40px] leading-none text-k-text">
              <span className="k-code" aria-hidden>&gt; </span>Sign in
            </h1>
            <p className="text-[15px] text-k-text/65">Use your 42 Studio account.</p>
          </div>

          <div className="mt-9 space-y-5">
            <div className="space-y-2 k-rise" style={{ animationDelay: "200ms" }}>
              <label htmlFor="signin-email" className="block text-[12.5px] font-bold uppercase tracking-[0.06em] text-k-text/80">
                Email
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-k-text/45 pointer-events-none" strokeWidth={1.8} aria-hidden />
                <input
                  ref={emailRef}
                  id="signin-email"
                  type="email"
                  inputMode="email"
                  autoFocus
                  autoComplete="email"
                  spellCheck={false}
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    if (error) setError(null);
                  }}
                  placeholder="you@smaa.com"
                  aria-invalid={!!error && !email.trim()}
                  className="input w-full h-12 pl-10 text-[15px]"
                />
              </div>
            </div>

            <div className="space-y-2 k-rise" style={{ animationDelay: "270ms" }}>
              <label htmlFor="signin-password" className="block text-[12.5px] font-bold uppercase tracking-[0.06em] text-k-text/80">
                Password
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-k-text/45 pointer-events-none" strokeWidth={1.8} aria-hidden />
                <input
                  ref={pwRef}
                  id="signin-password"
                  type={showPw ? "text" : "password"}
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    if (error) setError(null);
                  }}
                  onKeyUp={capsCheck}
                  onKeyDown={capsCheck}
                  onBlur={() => setCaps(false)}
                  aria-invalid={!!error && !!email.trim()}
                  aria-describedby={caps ? "signin-caps" : undefined}
                  className="input w-full h-12 pl-10 pr-12 text-[15px]"
                />
                <button
                  type="button"
                  onClick={() => setShowPw((v) => !v)}
                  aria-label={showPw ? "Hide password" : "Show password"}
                  aria-pressed={showPw}
                  className="absolute right-1 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center text-k-text/55 hover:text-k-text focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
                >
                  {showPw ? <EyeOff className="w-4 h-4" strokeWidth={1.8} aria-hidden /> : <Eye className="w-4 h-4" strokeWidth={1.8} aria-hidden />}
                </button>
              </div>
              {caps && (
                <p id="signin-caps" className="text-[12.5px] text-k-text/70 flex items-center gap-1.5">
                  <span className="px-1 py-px text-[10.5px] font-bold uppercase tracking-[0.06em] ring-1 ring-k-text/30">Caps Lock</span>
                  is on.
                </p>
              )}
            </div>

            {error && (
              <div
                key={attempt}
                id="signin-error"
                role="alert"
                className="k-shake flex items-start gap-2.5 px-3.5 py-3 border border-k-danger/60 bg-k-danger/10 text-[13.5px] text-k-text"
              >
                <AlertTriangle className="w-4 h-4 mt-px shrink-0 text-k-danger" strokeWidth={2} aria-hidden />
                <span>{error}</span>
              </div>
            )}
          </div>

          <div className="mt-8 k-rise" style={{ animationDelay: "340ms" }}>
            <button type="submit" disabled={busy} aria-busy={busy} className="k-btn w-full h-12 text-[14px] ml-[6px] max-w-[calc(100%-6px)]">
              {busy ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" strokeWidth={2.2} aria-hidden />
                  Signing in…
                </>
              ) : (
                <>
                  Sign in
                  <ArrowRight className="w-4 h-4" strokeWidth={2.2} aria-hidden />
                </>
              )}
            </button>
          </div>

          <p className="mt-7 text-[13px] text-k-text/55 leading-relaxed k-rise" style={{ animationDelay: "420ms" }}>
            Forgot your password, or need an account? Ask your 42 Studio admin.
          </p>
        </form>
      </main>
    </div>
  );
}
