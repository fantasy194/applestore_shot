import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

type Option = { value: string; label: string };

export function SelectField({
  label,
  value,
  options,
  onChange,
  disabled,
  autoFocus,
  required,
  placeholder = "请选择",
  className = "",
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
  autoFocus?: boolean;
  required?: boolean;
  placeholder?: string;
  className?: string;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const openedAt = useRef({ top: 0, left: 0 });
  const search = useRef({ text: "", time: 0 });
  const menuPointerDown = useRef(false);
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [position, setPosition] = useState<CSSProperties>({
    top: 0,
    left: 0,
    width: 0,
    maxHeight: 320,
  });
  const selected = options.findIndex((option) => option.value === value);

  const show = () => {
    if (disabled || !options.length) return;
    menuPointerDown.current = false;
    setCursor(Math.max(0, selected));
    search.current = { text: "", time: 0 };
    setOpen(true);
  };
  const choose = (index: number) => {
    if (!options[index]) return;
    menuPointerDown.current = false;
    onChange(options[index].value);
    setOpen(false);
    trigger.current?.focus();
  };
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  useEffect(() => {
    if (!options.length) setOpen(false);
    setCursor((index) => Math.max(0, Math.min(index, options.length - 1)));
  }, [options.length]);
  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return;
    const rect = trigger.current.getBoundingClientRect();
    openedAt.current = { top: rect.top, left: rect.left };
    const viewport = window.visualViewport;
    const viewportTop = viewport?.offsetTop ?? 0;
    const viewportLeft = viewport?.offsetLeft ?? 0;
    const viewportWidth = viewport?.width ?? innerWidth;
    const viewportBottom = viewportTop + (viewport?.height ?? innerHeight);
    const below = viewportBottom - rect.bottom - 14;
    const above = rect.top - viewportTop - 14;
    const upwards = below < 220 && above > below;
    const maxHeight = Math.min(320, Math.max(1, upwards ? above : below));
    const width = Math.min(rect.width, viewportWidth - 16);
    setPosition({
      ...(upwards
        ? { bottom: innerHeight - rect.top + 6 }
        : { top: rect.bottom + 6 }),
      left: Math.max(
        viewportLeft + 8,
        Math.min(rect.left, viewportLeft + viewportWidth - width - 8),
      ),
      width,
      maxHeight,
    });
  }, [open]);
  useLayoutEffect(() => {
    if (!open || !menu.current) return;
    const option = menu.current.children[cursor] as HTMLElement | undefined;
    if (!option) return;
    const bottom = option.offsetTop + option.offsetHeight;
    if (option.offsetTop < menu.current.scrollTop)
      menu.current.scrollTop = option.offsetTop;
    else if (bottom > menu.current.scrollTop + menu.current.clientHeight)
      menu.current.scrollTop = bottom - menu.current.clientHeight;
  }, [open, cursor, position]);
  useEffect(() => {
    if (!open) {
      menuPointerDown.current = false;
      return;
    }
    const outside = (event: Event) => {
      // Focusing an off-screen field can deliver a queued scroll after opening.
      // Keep the menu when its anchor has already reached this position.
      if (event.type === "scroll") {
        const rect = trigger.current?.getBoundingClientRect();
        if (
          rect &&
          Math.abs(rect.top - openedAt.current.top) < 1 &&
          Math.abs(rect.left - openedAt.current.left) < 1
        )
          return;
      }
      if (
        !trigger.current?.contains(event.target as Node) &&
        !menu.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const close = () => {
      menuPointerDown.current = false;
      setOpen(false);
    };
    const releasePointer = () => {
      if (!menuPointerDown.current) return;
      menuPointerDown.current = false;
      // A touch gesture can blur the trigger to body. Restore its place in the
      // keyboard order without moving the page or interrupting menu scrolling.
      trigger.current?.focus({ preventScroll: true });
    };
    const pointerUp = (event: PointerEvent) => {
      // Touch compatibility mouse events run after pointerup. Keep the blur
      // guard until their click chooses the option, or the tap is lost.
      if (
        event.pointerType === "mouse" ||
        !menu.current?.contains(event.target as Node)
      )
        releasePointer();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("pointerup", pointerUp);
    document.addEventListener("pointercancel", releasePointer);
    document.addEventListener("click", releasePointer);
    window.addEventListener("scroll", outside, true);
    window.addEventListener("resize", close);
    window.visualViewport?.addEventListener("resize", close);
    window.visualViewport?.addEventListener("scroll", close);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("pointerup", pointerUp);
      document.removeEventListener("pointercancel", releasePointer);
      document.removeEventListener("click", releasePointer);
      window.removeEventListener("scroll", outside, true);
      window.removeEventListener("resize", close);
      window.visualViewport?.removeEventListener("resize", close);
      window.visualViewport?.removeEventListener("scroll", close);
    };
  }, [open]);

  const keyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const { key } = event;
    if (key === "Tab") {
      setOpen(false);
      return;
    }
    if (key === "Escape") {
      if (open) {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(key)) {
      event.preventDefault();
      if (!open) show();
      if (key === "Home") setCursor(0);
      else if (key === "End") setCursor(options.length - 1);
      else if (open)
        setCursor(
          (i) =>
            (i + (key === "ArrowDown" ? 1 : -1) + options.length) %
            options.length,
        );
    } else if (key === "Enter" || key === " ") {
      event.preventDefault();
      if (open) choose(cursor);
      else show();
    } else if (
      key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      const text =
        Date.now() - search.current.time > 700
          ? key
          : search.current.text + key;
      search.current = { text, time: Date.now() };
      const match = options.findIndex((option) =>
        option.label.toLocaleLowerCase().startsWith(text.toLocaleLowerCase()),
      );
      if (match >= 0) {
        event.preventDefault();
        setOpen(true);
        setCursor(match);
      }
    }
  };

  return (
    <div className={`select-field ${className}`}>
      <label id={`${id}-label`} htmlFor={id}>
        {label}
      </label>
      <button
        ref={trigger}
        id={id}
        type="button"
        role="combobox"
        aria-labelledby={`${id}-label`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? `${id}-list` : undefined}
        aria-activedescendant={open ? `${id}-option-${cursor}` : undefined}
        aria-required={required}
        disabled={disabled || !options.length}
        autoFocus={autoFocus}
        className="select-trigger"
        data-open={open || undefined}
        data-placeholder={selected < 0 || undefined}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={keyDown}
        onBlur={() => {
          if (!menuPointerDown.current) setOpen(false);
        }}
      >
        <span>{options[selected]?.label || placeholder}</span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {open &&
        createPortal(
          <div
            ref={menu}
            id={`${id}-list`}
            className="select-menu"
            role="listbox"
            aria-labelledby={`${id}-label`}
            style={position}
            onPointerDown={(event) => {
              menuPointerDown.current = true;
              if (
                event.pointerType === "mouse" &&
                (event.target as Element).closest('[role="option"]')
              ) {
                event.preventDefault();
              }
            }}
          >
            {options.map((option, index) => (
              <div
                key={option.value}
                id={`${id}-option-${index}`}
                role="option"
                aria-selected={index === selected}
                data-highlighted={index === cursor || undefined}
                className="select-option"
                onPointerMove={() => setCursor(index)}
                onClick={() => choose(index)}
              >
                <span>{option.label}</span>
                {index === selected && <Check size={15} aria-hidden="true" />}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
