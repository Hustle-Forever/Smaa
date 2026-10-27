import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Jost: a free Futura-style face. 42 Abu Dhabi's brand face is Futura PT.
        sans: [
          '"Jost"',
          '"Futura PT"',
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
        // The app-wide tokens every screen paints with. They read the 42 theme
        // variables (styles.css), so 42 Dark / 42 Light reach every page, the
        // ones nobody has redesigned included, without touching their code.
        bg: {
          DEFAULT: "rgb(var(--k-canvas) / <alpha-value>)",
          subtle: "rgb(var(--k-surface) / <alpha-value>)",
          muted: "rgb(var(--k-inset) / <alpha-value>)",
          deep: "rgb(var(--k-inset) / <alpha-value>)",
        },
        fg: "rgb(var(--k-text) / <alpha-value>)",
        muted: "rgb(var(--k-muted) / <alpha-value>)",
        border: {
          DEFAULT: "rgb(var(--k-border) / <alpha-value>)",
          strong: "rgb(var(--k-border-strong) / <alpha-value>)",
        },
        // Brand accent. A CSS variable (set in styles.css) rather than a fixed
        // hex, so a redesigned area can re-point it without touching the ~250
        // `*-accent` classes inside it: `.studio-ui` turns it to ink. Everywhere
        // else it is still the v1 teal #06B6A2 / #048876, byte for byte.
        accent: {
          DEFAULT: "rgb(var(--accent-rgb) / <alpha-value>)",
          hover: "rgb(var(--accent-hover-rgb) / <alpha-value>)",
          fade: "rgb(var(--accent-rgb) / 0.08)",
        },
        // 42 Abu Dhabi brand, after 42abudhabi.ae: charcoal, white, cyan
        // #00E5FC, purple #7100FE. Every value is a CSS variable set per theme
        // (html[data-k="dark" | "light"], see styles.css), so one class works
        // in both themes and keeps Tailwind's /opacity modifiers.
        k: {
          canvas: "rgb(var(--k-canvas) / <alpha-value>)",
          surface: "rgb(var(--k-surface) / <alpha-value>)",
          border: "rgb(var(--k-border) / <alpha-value>)",
          "border-strong": "rgb(var(--k-border-strong) / <alpha-value>)",
          muted: "rgb(var(--k-muted) / <alpha-value>)",
          btn: "rgb(var(--k-btn) / <alpha-value>)",
          onbtn: "rgb(var(--k-onbtn) / <alpha-value>)",
          coral: "rgb(var(--k-coral) / <alpha-value>)",
          inset: "rgb(var(--k-inset) / <alpha-value>)",
          line: "rgb(var(--k-line) / <alpha-value>)",
          text: "rgb(var(--k-text) / <alpha-value>)",
          cyan: "rgb(var(--k-cyan) / <alpha-value>)",
          oncyan: "rgb(var(--k-oncyan) / <alpha-value>)",
          // Readable accent for TEXT and small icons: cyan on charcoal,
          // purple on white (cyan on white is 1.4:1).
          accent: "rgb(var(--k-accent) / <alpha-value>)",
          purple: "rgb(var(--k-purple) / <alpha-value>)",
          chart: "rgb(var(--k-chart) / <alpha-value>)",
          danger: "rgb(var(--k-danger) / <alpha-value>)",
          rail: "rgb(var(--k-rail) / <alpha-value>)",
        },
        // Institutional gold: the single accent of the redesigned (government
        // office) surfaces, after the UAE Government Design System's gold.
        // DEFAULT is for marks and fills; `ink` is the text-safe shade (5:1 on
        // white); `soft` is a tinted background.
        gold: {
          // Was institutional gold; now the 42 cyan (marks) and the theme's
          // readable accent (text), so older classes follow the brand.
          DEFAULT: "rgb(var(--k-cyan) / <alpha-value>)",
          ink: "rgb(var(--k-accent) / <alpha-value>)",
          soft: "rgb(var(--k-cyan) / 0.14)",
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
      // 42abudhabi.ae is square: 441 of 444 elements measured at 0px radius.
      // The whole scale collapses to hairline corners; `full` stays round for
      // avatars, status dots and progress rings.
      borderRadius: {
        none: "0",
        sm: "1px",
        DEFAULT: "2px",
        md: "2px",
        lg: "2px",
        xl: "3px",
        "2xl": "3px",
        "3xl": "4px",
        full: "9999px",
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
        // Dashboard chart: the line draws itself left to right. Needs the path
        // to carry pathLength={1} and strokeDasharray={1}.
        drawLine: {
          "0%": { strokeDashoffset: "1" },
          "100%": { strokeDashoffset: "0" },
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
        drawLine: "drawLine 1400ms cubic-bezier(0.65, 0, 0.35, 1) both",
        // Slower, softer section entrance for the Dashboard's staggered reveal.
        reveal: "riseFade 600ms cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [],
} satisfies Config;
