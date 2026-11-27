export const usd = (n: number | null | undefined) =>
  n == null ? "–" : n.toLocaleString("en-US", { style: "currency", currency: "USD" });

export const num = (n: number) => n.toLocaleString("en-US");

export const label = (s: string) => s.replace(/[._]/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function when(iso: string | null | undefined) {
  if (!iso) return "–";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 0 && diff > -3600 * 24) return `in ${duration(-diff)}`;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

export function duration(seconds: number) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export const FINAL_TICKET_STATES = ["completed", "failed", "dead_lettered"];

export const ASSET_COLORS: Record<string, string> = {
  Stocks: "var(--c-stocks)",
  Bonds: "var(--c-bonds)",
  Cash: "var(--c-cash)",
  "Real Estate": "var(--c-realestate)",
  Commodities: "var(--c-commodities)",
};
