import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"Inter"',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'sans-serif',
        ],
        mono: [
          '"JetBrains Mono"',
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      // Bare strings, NOT [size, lineHeight] tuples. Tailwind only emits a
      // line-height when the value is an array, so `text-2xs` compiles to
      // exactly `font-size: 11px` -- byte-identical to the `text-[11px]`
      // literal it replaces. That is what makes swapping 455 call sites a
      // rename rather than a restyle. For the same reason `text-[12px]` is
      // deliberately NOT folded into `text-xs`: text-xs also sets
      // line-height: 1rem, which would move things.
      fontSize: {
        "3xs": "10px",
        "2xs": "11px",
      },
      colors: {
        // Surfaces — keeping the `bg.*` keys so existing classes don't break;
        // tokens now point at the v1 white-on-near-black-text palette.
        bg: {
          DEFAULT: "#FFFFFF",
          subtle: "#FFFFFF",
          muted: "#F2F2F4",
          deep: "#FAFAF9",
        },
        // Text
        fg: "#0F1011",
        muted: "#71717A",
        // Borders
        border: {
          DEFAULT: "#E5E5E7",
          strong: "#D4D4D8",
        },
        // Brand accent (v1 teal)
        accent: {
          DEFAULT: "#06B6A2",
          hover: "#048876",
          fade: "rgba(6,182,162,0.08)",
        },
        // Semantics
        success: "#10B981",
        warning: "#F59E0B",
        danger: "#EF4444",
        // RunPanel state tints
        state: {
          running: "rgba(6,182,162,0.06)",
          completed: "rgba(16,185,129,0.08)",
          failed: "rgba(239,68,68,0.08)",
        },
      },
      borderRadius: {
        DEFAULT: "0.5rem",
      },
      boxShadow: {
        soft: "0 1px 2px rgba(24,24,27,0.04), 0 0 0 1px rgba(24,24,27,0.04)",
        lift: "0 10px 30px -10px rgba(24,24,27,0.10), 0 4px 8px -4px rgba(24,24,27,0.06)",
        glow: "0 0 0 6px rgba(6,182,162,0.10)",
      },
      letterSpacing: {
        wider2: "0.16em",
        wider3: "0.2em",
      },
      keyframes: {
        ringPulse: {
          "0%": { boxShadow: "0 0 0 0 rgba(6,182,162,0.55)" },
          "70%": { boxShadow: "0 0 0 10px rgba(6,182,162,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(6,182,162,0)" },
        },
        scaleIn: {
          "0%": { transform: "scale(0.3)", opacity: "0" },
          "100%": { transform: "scale(1)", opacity: "1" },
        },
        slideInRight: {
          "0%": { transform: "translateX(100%)" },
          "100%": { transform: "translateX(0)" },
        },
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        riseFade: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        countUp: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pageEnter: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseGlow: {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(6,182,162,0)" },
          "50%": { boxShadow: "0 0 0 8px rgba(6,182,162,0.18)" },
        },
        // The two progress motions, which four creators each re-declared with a
        // hand-written <style> tag inside JSX. They are NOT the same animation
        // and must not be merged: `indeterminate` sweeps a 30%-wide bar across
        // an overflow-hidden track (hence 370%), `sheen` slides a highlight
        // across a button (hence 100%). PosterHumanCreator and
        // TalkingHeadV2Creator both called theirs `yasProgress` with these two
        // different bodies -- and since @keyframes are global and both live
        // under the kept-mounted Producer slot, whichever mounted last silently
        // overrode the other.
        indeterminate: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(370%)" },
        },
        sheen: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        dotPing: {
          "0%": { transform: "scale(1)", opacity: "0.5" },
          "75%, 100%": { transform: "scale(2)", opacity: "0" },
        },
        // Expanding rings behind the sign-in screen's brand mark.
        haloPulse: {
          "0%": { transform: "scale(0.85)", opacity: "0.45" },
          "100%": { transform: "scale(2.4)", opacity: "0" },
        },
      },
      animation: {
        ringPulse: "ringPulse 1.4s ease-out infinite",
        scaleIn: "scaleIn 240ms cubic-bezier(0.34, 1.56, 0.64, 1)",
        slideInRight: "slideInRight 280ms cubic-bezier(0.16, 1, 0.3, 1)",
        fadeIn: "fadeIn 200ms ease-out",
        shimmer: "shimmer 1.6s linear infinite",
        riseFade: "riseFade 320ms cubic-bezier(0.16, 1, 0.3, 1) both",
        countUp: "countUp 220ms cubic-bezier(0.16, 1, 0.3, 1) both",
        pageEnter: "pageEnter 220ms cubic-bezier(0.16, 1, 0.3, 1) both",
        pulseGlow: "pulseGlow 2.2s ease-in-out infinite",
        dotPing: "dotPing 2s cubic-bezier(0, 0, 0.2, 1) infinite",
        indeterminate: "indeterminate 1.5s ease-in-out infinite",
        sheen: "sheen 1.5s ease-in-out infinite",
        haloPulse: "haloPulse 3s cubic-bezier(0.16, 1, 0.3, 1) infinite backwards",
      },
    },
  },
  plugins: [],
} satisfies Config;
