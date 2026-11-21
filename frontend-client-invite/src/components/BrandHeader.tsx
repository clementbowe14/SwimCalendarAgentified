import { CalendarIcon } from "@radix-ui/react-icons";

type BrandHeaderProps = {
  eyebrow: string;
  title: string;
  copy: string;
  businessName: string;
};

export function BrandHeader({ eyebrow, title, copy, businessName }: BrandHeaderProps) {
  return (
    <section className="client-hero">
      <div className="brand-row">
        <span className="brand-mark">SC</span>
        <span>{businessName}</span>
      </div>
      <div className="pool-visual" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      <p>{copy}</p>
      <div className="hero-proof">
        <CalendarIcon />
        <span>Only appointment times that are available to request are shown.</span>
      </div>
    </section>
  );
}

