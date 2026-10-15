import type { AnalysisStatus } from "@/lib/api";

// docs/07: every claim renders with its status chip; the statuses are visually
// distinct (colour, border style and label), never a tooltip-only signal.
const STYLES: Record<AnalysisStatus, string> = {
  verified: "border-emerald-600/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  inferred: "border-amber-600/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  unknown: "border-dashed border-rose-600/50 bg-rose-500/10 text-rose-700 dark:text-rose-400",
  unanalyzed: "border-zinc-500/40 bg-zinc-500/10 text-zinc-600 dark:text-zinc-400",
};

const LABELS: Record<AnalysisStatus, string> = {
  verified: "Verified",
  inferred: "Inferred",
  unknown: "Unknown",
  unanalyzed: "Unanalyzed",
};

export function StatusChip({ status }: { status: AnalysisStatus }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-px text-[10px] font-medium uppercase leading-4 tracking-wide ${STYLES[status]}`}
    >
      {LABELS[status]}
    </span>
  );
}
