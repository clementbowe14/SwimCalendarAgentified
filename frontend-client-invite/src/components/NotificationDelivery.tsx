import type { NotificationStatus } from "../api/types";

const labels: Record<NotificationStatus["status"], string> = {
  queued: "queued", processing: "processing", accepted: "accepted by provider (delivery unconfirmed)",
  delivered: "delivered", failed: "failed", skipped: "skipped",
};

export function NotificationDelivery({ notifications }: { notifications: NotificationStatus[] }) {
  return <div className="delivery-row" aria-live="polite">
    {notifications.filter((item) => item.kind === "confirmation").map((item) => (
      <span className="delivery-chip" key={item.channel}>{item.channel === "sms" ? "Text" : "Email"}: {labels[item.status]}</span>
    ))}
  </div>;
}
