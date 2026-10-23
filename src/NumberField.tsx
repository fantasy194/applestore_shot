import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Minus, Plus } from "lucide-react";

export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  disabled,
  required = true,
  className = "",
  hint,
  onValidityChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  required?: boolean;
  className?: string;
  hint?: ReactNode;
  onValidityChange?: (valid: boolean) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(String(value));
  const validityCallback = useRef(onValidityChange);
  validityCallback.current = onValidityChange;
  const numeric = draft.trim() === "" ? NaN : Number(draft);
  const valid =
    Number.isFinite(numeric) &&
    numeric >= min &&
    (max === undefined || numeric <= max) &&
    Math.abs((numeric - min) / step - Math.round((numeric - min) / step)) <
      0.0000001;

  useEffect(() => setDraft(String(value)), [value]);
  useEffect(() => {
    validityCallback.current?.(valid);
  }, [valid]);

  const normalize = (candidate: number) => {
    const fallback = Number.isFinite(value) ? value : min;
    const bounded = Math.max(
      min,
      Math.min(
        max ?? Infinity,
        Number.isFinite(candidate) ? candidate : fallback,
      ),
    );
    const steps = Math.min(
      Math.round((bounded - min) / step),
      max === undefined ? Infinity : Math.floor((max - min) / step),
    );
    return Number((min + steps * step).toFixed(8));
  };
  const commit = (next: number) => {
    setDraft(String(next));
    validityCallback.current?.(true);
    if (next !== value) onChange(next);
  };

  return (
    <div className={`number-field ${className}`}>
      <label htmlFor={id}>{label}</label>
      <div className="number-control">
        <button
          type="button"
          aria-label={`减少${label}`}
          disabled={disabled || (valid && numeric <= min)}
          onClick={() => commit(normalize(normalize(numeric) - step))}
        >
          <Minus size={16} aria-hidden="true" />
        </button>
        <input
          id={id}
          type="number"
          inputMode={Number.isInteger(step) ? "numeric" : "decimal"}
          min={min}
          max={max}
          step={step}
          required={required}
          disabled={disabled}
          value={draft}
          aria-invalid={!valid || undefined}
          aria-describedby={hint ? `${id}-hint` : undefined}
          ref={(input) => {
            input?.setCustomValidity(valid ? "" : "请输入范围内的有效数字");
          }}
          onChange={(event) => {
            const next = event.target.value;
            setDraft(next);
            const number = event.target.valueAsNumber;
            event.target.setCustomValidity("");
            const nextValid = event.target.validity.valid && next !== "";
            validityCallback.current?.(nextValid);
            if (nextValid && Number.isFinite(number)) onChange(number);
          }}
          onBlur={() => commit(normalize(numeric))}
        />
        <button
          type="button"
          aria-label={`增加${label}`}
          disabled={disabled || (valid && max !== undefined && numeric >= max)}
          onClick={() => commit(normalize(normalize(numeric) + step))}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
      </div>
      {hint && <span id={`${id}-hint`}>{hint}</span>}
    </div>
  );
}
