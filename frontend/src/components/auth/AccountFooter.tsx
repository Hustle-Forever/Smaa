import { useState } from "react";
import { LogOut, Moon, Sun } from "lucide-react";

import { currentTheme, setTheme, type StudioTheme } from "../../lib/theme";

import { useCurrentUser, useSignOut } from "./AuthGate";

/**
 * Who is signed in, and the way out.
 *
 * Extracted rather than copied for the same reason NavList is: the desktop rail
 * and the mobile drawer both need it, and a second copy is how the two quietly
 * drift apart.
 */
export function AccountFooter({ onSignedOut }: { onSignedOut?: () => void }) {
  const user = useCurrentUser();
  const signOut = useSignOut();
  const [theme, setThemeState] = useState<StudioTheme>(() => currentTheme());
  const pick = (t: StudioTheme) => {
    setTheme(t);
    setThemeState(t);
  };
  return (
    <div className="shrink-0 p-3 border-t border-white/[0.08] space-y-2.5">
      {/* 42 Dark / 42 Light. Remembered in this browser (lib/theme.ts). */}
      <div className="grid grid-cols-2 gap-0.5 p-0.5 bg-white/[0.06] ring-1 ring-white/[0.08]" role="group" aria-label="Theme">
        {(["dark", "light"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => pick(t)}
            aria-pressed={theme === t}
            className={[
              "h-8 inline-flex items-center justify-center gap-1.5 text-[11.5px] font-bold uppercase tracking-[0.08em] transition-colors",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70",
              theme === t ? "bg-k-cyan text-k-oncyan" : "text-white/60 hover:text-white",
            ].join(" ")}
          >
            {t === "dark" ? <Moon className="w-3.5 h-3.5" strokeWidth={2} aria-hidden /> : <Sun className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />}
            {t === "dark" ? "Dark" : "Light"}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2.5 bg-white/[0.06] ring-1 ring-white/[0.08] p-2 pl-2.5">
        <span className="inline-flex w-9 h-9 shrink-0 items-center justify-center bg-k-cyan text-k-oncyan k-display text-[14px] leading-none">
          {user.initials}
        </span>
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-[13px] font-semibold text-white">
            {user.name}
          </span>
          <span className="block truncate text-[11.5px] text-white/55">{user.email}</span>
        </span>
        <button
          type="button"
          onClick={() => {
            onSignedOut?.();
            signOut();
          }}
          className="w-9 h-9 shrink-0 rounded-lg flex items-center justify-center text-white/60 hover:text-white hover:bg-white/[0.08] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-k-cyan/70"
          aria-label="Sign out"
          title="Sign out"
        >
          <LogOut className="w-4 h-4" strokeWidth={1.8} aria-hidden />
        </button>
      </div>
    </div>
  );
}
