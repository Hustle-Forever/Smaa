interface Props {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}

export function Toggle({ label, checked, onChange, disabled }: Props) {
  return (
    <label
      className={[
        "inline-flex items-center gap-2 cursor-pointer select-none text-sm",
        disabled ? "opacity-50 cursor-not-allowed" : "",
      ].join(" ")}
    >
      <span
        className={[
          "inline-flex w-9 h-5 rounded-full p-0.5 transition-colors",
          checked ? "bg-accent" : "bg-border",
        ].join(" ")}
        role="switch"
        aria-checked={checked}
      >
        <span
          className={[
            "inline-block w-4 h-4 rounded-full bg-white shadow-sm transition-transform",
            checked ? "translate-x-4" : "translate-x-0",
          ].join(" ")}
        />
      </span>
      <input
        type="checkbox"
        className="sr-only"
        checked={checked}
        onChange={(e) => onChange(e.currentTarget.checked)}
        disabled={disabled}
      />
      <span>{label}</span>
    </label>
  );
}
