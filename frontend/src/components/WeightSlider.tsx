interface Props {
  label: string;
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  hint?: string;
}

export function WeightSlider({ label, value, onChange, disabled, hint }: Props) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <label
      className={[
        "block py-2",
        disabled ? "opacity-50 pointer-events-none" : "",
      ].join(" ")}
    >
      <div className="flex items-baseline justify-between mb-1.5">
        <span className="text-sm font-medium text-fg">{label}</span>
        <span className="font-mono text-sm text-fg tabular-nums">
          {pct}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        value={pct}
        className="slider w-full"
        style={{ ["--pct" as any]: `${pct}%` }}
        onChange={(e) => onChange(Number(e.currentTarget.value))}
        disabled={disabled}
      />
      {hint && <p className="font-mono text-2xs text-muted mt-1">{hint}</p>}
    </label>
  );
}
