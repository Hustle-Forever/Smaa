// Rebrand studio — "Pick": any colour, in one click.
//
// A label with the native colour input laid over it (invisible), so the
// first click opens the browser's picker. While the picker is dragged only
// this swatch follows; the choice is committed on the input's native
// `change` (the picker closing), not on every `input` tick, so one pick is
// one edit, one undo step and one preview.

import { useEffect, useRef, useState } from "react";

const RAINBOW = "conic-gradient(#ff4d4d, #ffd84d, #5cff5c, #4dd2ff, #6b4dff, #ff4dd2, #ff4d4d)";

export function PickColour({
  value,
  checked,
  onCommit,
  label,
  title,
  disabled = false,
}: {
  /** The colour it holds (shown only while it is the chosen one). */
  value: string | null;
  /** This is the current choice (a colour none of the swatches has). */
  checked: boolean;
  onCommit: (hex: string) => void;
  label: string;
  title?: string;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const [live, setLive] = useState<string | null>(null);
  const onCommitRef = useRef(onCommit);
  onCommitRef.current = onCommit;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onInput = () => setLive(el.value);
    const onChange = () => {
      setLive(null);
      onCommitRef.current(el.value.toLowerCase());
    };
    el.addEventListener("input", onInput);
    el.addEventListener("change", onChange);
    return () => {
      el.removeEventListener("input", onInput);
      el.removeEventListener("change", onChange);
    };
  }, []);

  // The picker opens on the colour in use.
  useEffect(() => {
    if (ref.current && value) ref.current.value = value;
  }, [value]);

  const dot = live ?? (checked ? value : null);
  return (
    <label
      title={title}
      className={`relative inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border pl-1 pr-2.5 text-2xs transition-colors focus-within:ring-2 focus-within:ring-accent/40 ${
        disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"
      } ${checked ? "border-accent bg-accent-fade text-accent-hover font-medium" : "border-border text-muted hover:text-fg hover:border-border-strong"}`}
    >
      <span className="h-5 w-5 rounded-full border border-black/10" style={{ background: dot ?? RAINBOW }} aria-hidden />
      {label}
      <input
        ref={ref}
        type="color"
        defaultValue={value ?? "#7f2bf2"}
        disabled={disabled}
        aria-label={title ?? label}
        className="absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
      />
    </label>
  );
}

/** A filled round swatch (the icon of a swatch-variant ChoiceChips option). */
export function SwatchDot({ colour, mark }: { colour: string | null; mark?: string }) {
  return (
    <span
      aria-hidden
      className="flex h-6 w-6 items-center justify-center rounded-full border border-black/10 text-3xs font-bold text-white [text-shadow:0_0_2px_rgba(0,0,0,0.6)]"
      style={{ background: colour ?? RAINBOW }}
    >
      {mark}
    </span>
  );
}
