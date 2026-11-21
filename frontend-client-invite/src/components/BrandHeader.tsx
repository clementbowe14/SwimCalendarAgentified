import { CalendarIcon } from "@radix-ui/react-icons";
import type { RefObject } from "react";

type BrandHeaderProps = {
  eyebrow: string;
  title: string;
  copy: string;
  businessName: string;
  titleRef: RefObject<HTMLHeadingElement | null>;
};

export function BrandHeader({ eyebrow, title, copy, businessName, titleRef }: BrandHeaderProps) {
  return (
    <section className="client-hero">
      <div className="brand-row">
        <span className="brand-mark" aria-hidden="true">
          <img src="/assets/dayweave-mark.png" alt="" />
        </span>
        <span className="brand-copy">
          <strong>Dayweave</strong>
          <small>Scheduling for <span>{businessName}</span></small>
        </span>
      </div>
      <div className="weave-visual" aria-hidden="true">
        <span className="weave-kicker">A clear way to coordinate</span>
        <strong>One invitation.<br />A time that works.</strong>
        <span className="weave-visual-icon"><CalendarIcon /></span>
      </div>
      <p className="eyebrow">{eyebrow}</p>
      <h1 ref={titleRef} tabIndex={-1}>{title}</h1>
      <p>{copy}</p>
      <div className="hero-proof">
        <CalendarIcon />
        <span>Only appointment times that are available to request are shown.</span>
      </div>
    </section>
  );
}

