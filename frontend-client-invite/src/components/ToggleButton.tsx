import type { ButtonHTMLAttributes, ReactNode } from "react";

type ToggleButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  selected: boolean;
  children: ReactNode;
};

export function ToggleButton({ selected, children, className = "", ...props }: ToggleButtonProps) {
  return (
    <button
      className={`${className} ${selected ? "selected" : ""}`.trim()}
      type="button"
      aria-pressed={selected}
      {...props}
    >
      {children}
    </button>
  );
}

