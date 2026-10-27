import { LogOut } from "lucide-react";

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
  return (
    <div className="shrink-0 px-3 py-4 border-t border-border space-y-2">
      <div className="flex items-center gap-2.5 px-2">
        <span className="inline-flex w-8 h-8 shrink-0 items-center justify-center rounded-full bg-accent/10 text-accent text-[11px] font-bold leading-none">
          {user.initials}
        </span>
        <span className="min-w-0 leading-tight">
          <span className="block truncate text-[13px] font-semibold text-fg">
            {user.name}
          </span>
          <span className="block truncate text-[11px] text-muted">{user.email}</span>
        </span>
      </div>
      <button
        type="button"
        onClick={() => {
          onSignedOut?.();
          signOut();
        }}
        className="nav-item w-full"
      >
        <LogOut className="nav-icon" strokeWidth={1.8} aria-hidden />
        <span>Sign out</span>
      </button>
    </div>
  );
}
