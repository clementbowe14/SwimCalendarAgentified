import type { ReactNode } from "react";

type SectionCardProps = {
  eyebrow?: string;
  title: string;
  meta?: string;
  children: ReactNode;
  className?: string;
};

export function SectionCard({ eyebrow, title, meta, children, className = "" }: SectionCardProps) {
  return (
    <section className={`section-card ${className}`}>
      <div className="section-heading row">
        <div>
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h2>{title}</h2>
        </div>
        {meta ? <span className="length-pill">{meta}</span> : null}
      </div>
      {children}
    </section>
  );
}

