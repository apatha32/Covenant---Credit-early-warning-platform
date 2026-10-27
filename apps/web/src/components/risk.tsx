import { TIERS } from "@/lib/format";
import type { Tier } from "@/lib/types";
import { cn } from "@/lib/utils";

export function TierBadge({ tier, className }: { tier: Tier; className?: string }) {
  const t = TIERS[tier];
  return (
    <span
      title={t.hint}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-semibold whitespace-nowrap",
        t.className,
        className,
      )}
    >
      <span className="size-1.5 rounded-full" style={{ background: t.dot }} aria-hidden />
      {t.label}
    </span>
  );
}

/** Last few probabilities, oldest first. Scaled to its own range, so read the shape, not height. */
export function Sparkline({ values, tier }: { values: number[]; tier: Tier }) {
  const w = 84;
  const h = 24;
  if (values.length < 2) return <span className="text-xs text-muted-foreground">—</span>;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (w - 4) + 2, h - 3 - ((v - min) / span) * (h - 6)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lx, ly] = pts[pts.length - 1];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Recent score trend">
      <path
        d={d}
        fill="none"
        stroke="var(--muted-foreground)"
        strokeOpacity={0.55}
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
      <circle cx={lx} cy={ly} r={2.6} fill={TIERS[tier].dot} />
    </svg>
  );
}
